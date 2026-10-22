"""Tool policy: the code layer between the model and every tool (baseline AG-02 to AG-06, AP-03..10).

The model proposes tool calls; this plugin decides whether they run. It never trusts the model
for identity or authority:

- Only allow-listed tools run, with only their declared arguments, within size limits.
- Identity never comes from the model: no argument may be named like, or contain, a phone,
  MyKad or account number. Tools read the caller's line from session state set by code.
- Write tools (anything that changes an account) also need:
    * verification level >= the tool's minimum, set only by code (set_verification_level);
    * a single-use confirmation token, issued by code after the read-back, bound to the exact
      arguments and valid for 120 seconds (issue_confirmation);
    * no prompt-injection flag on this call, write tools enabled (WRITE_TOOLS_ENABLED and the
      per-call flag Genesys can pass), and fewer than the per-call write limit.
- Tool results are trimmed before the model sees them.

A refusal is returned to the model as a tool result with `next_step`, so it says it can't do
that and offers a person; nothing is executed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin

from .events import security_event

STATE_PREFIX = "guardrails:"
K_VERIFICATION = STATE_PREFIX + "verification_level"
K_INJECTION = STATE_PREFIX + "injection_flag"
K_WRITES_ENABLED = STATE_PREFIX + "write_tools_enabled"
K_WRITES = STATE_PREFIX + "writes"
K_CONFIRMATIONS = STATE_PREFIX + "confirmations"
K_CALLS = STATE_PREFIX + "tool_calls"

CONFIRMATION_TTL_S = 120
# Argument names that would let the model choose whose account is touched.
IDENTITY_ARG_NAMES = frozenset({
    "msisdn", "phone", "phone_number", "number", "caller", "caller_id", "caller_number",
    "line", "line_number", "mobile", "account", "account_id", "account_number", "customer_id",
    "ic", "ic_number", "mykad", "nric", "passport",
})
# Values that look like Malaysian phone numbers or MyKad numbers.
_IDENTITY_VALUE = re.compile(
    r"(?<!\d)(?:\+?6)?01[0-46-9][-\s]?\d{3,4}[-\s]?\d{4}(?!\d)"
    r"|(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])-?\d{2}-?\d{4}(?!\d)"
)
CONTROL_ARGS = frozenset({"confirmed", "confirmation_token"})


@dataclass(frozen=True)
class ToolRule:
  name: str
  write: bool = False
  args: frozenset[str] = frozenset()
  max_arg_chars: int = 300
  min_verification: int = 0

  def __post_init__(self):
    bad = {a for a in self.args if a.lower() in IDENTITY_ARG_NAMES}
    if bad:
      raise ValueError(f"{self.name}: identity must come from session state, not arguments {sorted(bad)}")
    if self.write and self.min_verification < 2:
      raise ValueError(f"{self.name}: write tools need verification level 2")


@dataclass
class ToolPolicy:
  rules: dict[str, ToolRule] = field(default_factory=dict)
  max_writes_per_call: int = 1
  max_tool_calls_per_call: int = 40
  max_result_chars: int = 1500
  max_result_items: int = 10

  @classmethod
  def default(cls) -> "ToolPolicy":
    """The tools the agent has today. Add each new tool here when it is built."""
    return cls(rules={
        "search_knowledge_base": ToolRule("search_knowledge_base", args=frozenset({"query"}), max_arg_chars=300),
    })

  def with_rule(self, rule: ToolRule) -> "ToolPolicy":
    return ToolPolicy({**self.rules, rule.name: rule}, self.max_writes_per_call,
                      self.max_tool_calls_per_call, self.max_result_chars, self.max_result_items)


# ----------------------------------------------------------------------------- state helpers
# Called by code (OTP check, Genesys input variables), never reachable by the model.

def set_verification_level(state, level: int, source: str) -> None:
  """Raises the caller's verification level. Only code paths call this (OTP, IVR status)."""
  if level not in (0, 1, 2):
    raise ValueError("verification level must be 0, 1 or 2")
  current = int(state.get(K_VERIFICATION, 0) or 0)
  state[K_VERIFICATION] = max(current, level)
  security_event("verification_level_set", verification_level=state[K_VERIFICATION], source=source)


def flag_injection(state, source: str, category: str = "") -> None:
  if not state.get(K_INJECTION):
    state[K_INJECTION] = True
  security_event("injection_flag", source=source, category=category or None, severity="WARNING")


def _args_digest(tool: str, args: dict) -> str:
  clean = {k: v for k, v in sorted(args.items()) if k not in CONTROL_ARGS}
  return hashlib.sha256(json.dumps([tool, clean], sort_keys=True, default=str).encode()).hexdigest()


def issue_confirmation(state, tool: str, args: dict, *, now: Optional[float] = None) -> str:
  """Issues a single-use token after the read-back of exactly these arguments (AP-07).

  A write tool's preview step calls this and returns the token to the model with its
  say_as summary; the confirmed call must carry it, unchanged arguments included.
  """
  now = time.time() if now is None else now
  token = secrets.token_urlsafe(16)
  pending = {t: v for t, v in dict(state.get(K_CONFIRMATIONS) or {}).items() if v["exp"] > now}
  pending[token] = {"tool": tool, "digest": _args_digest(tool, args), "exp": now + CONFIRMATION_TTL_S}
  state[K_CONFIRMATIONS] = pending
  return token


def _consume_confirmation(state, tool: str, args: dict, now: float) -> Optional[str]:
  """Returns None if valid (and consumes it), else a reason code."""
  token = args.get("confirmation_token")
  pending = dict(state.get(K_CONFIRMATIONS) or {})
  entry = pending.pop(token, None) if isinstance(token, str) else None
  state[K_CONFIRMATIONS] = pending  # single use: removed whatever the outcome
  if entry is None:
    return "confirmation_missing"
  if entry["exp"] <= now:
    return "confirmation_expired"
  if entry["tool"] != tool or entry["digest"] != _args_digest(tool, args):
    return "confirmation_mismatch"
  return None


def _env_writes_enabled() -> bool:
  return os.getenv("WRITE_TOOLS_ENABLED", "true").lower() == "true"


def _refuse(reason: str, next_step: str = "offer_agent") -> dict:
  return {"status": "refused", "reason": reason, "next_step": next_step,
          "message": "This can't be done on this call. Offer to connect the caller to a person."}


# ----------------------------------------------------------------------------- plugin

class ToolPolicyPlugin(BasePlugin):

  def __init__(self, policy: Optional[ToolPolicy] = None, name: str = "tool_policy"):
    super().__init__(name)
    self.policy = policy or ToolPolicy.default()

  def check(self, tool_name: str, args: dict, state, *, now: Optional[float] = None) -> Optional[dict]:
    """Returns None to allow the call, or the refusal to return instead. Pure: usable in tests."""
    now = time.time() if now is None else now
    rule = self.policy.rules.get(tool_name)
    if rule is None:
      return self._deny(tool_name, "tool_not_allowed")

    calls = int(state.get(K_CALLS, 0) or 0) + 1
    state[K_CALLS] = calls
    if calls > self.policy.max_tool_calls_per_call:
      return self._deny(tool_name, "tool_call_limit")

    extra = set(args) - rule.args - (CONTROL_ARGS if rule.write else set())
    if extra:
      return self._deny(tool_name, "unexpected_arguments")
    for key, value in args.items():
      if key.lower() in IDENTITY_ARG_NAMES:
        return self._deny(tool_name, "identity_in_arguments")
      if isinstance(value, str):
        if len(value) > rule.max_arg_chars:
          return self._deny(tool_name, "argument_too_long")
        if _IDENTITY_VALUE.search(value):
          return self._deny(tool_name, "identity_in_arguments")
      elif not isinstance(value, (int, float, bool)) and value is not None:
        return self._deny(tool_name, "argument_type")

    if not rule.write:
      return None

    # --- write tools
    if not args.get("confirmed"):
      return None  # preview step: no side effect; the tool issues a confirmation token
    if not _env_writes_enabled() or state.get(K_WRITES_ENABLED) is False:
      return self._deny(tool_name, "writes_disabled")
    if state.get(K_INJECTION):
      return self._deny(tool_name, "injection_flag")
    level = int(state.get(K_VERIFICATION, 0) or 0)
    if level < rule.min_verification:
      return self._deny(tool_name, "verification_required", next_step="verify_caller", level=level)
    if int(state.get(K_WRITES, 0) or 0) >= self.policy.max_writes_per_call:
      return self._deny(tool_name, "write_limit")
    problem = _consume_confirmation(state, tool_name, args, now)
    if problem:
      return self._deny(tool_name, problem, next_step="read_back_again")
    state[K_WRITES] = int(state.get(K_WRITES, 0) or 0) + 1
    security_event("write_action", tool=tool_name, result="allowed", verification_level=level)
    return None

  def _deny(self, tool: str, reason: str, next_step: str = "offer_agent", level: Optional[int] = None) -> dict:
    security_event("policy_denied", tool=tool, result="refused", reason=reason,
                   verification_level=level, severity="WARNING")
    return _refuse(reason, next_step)

  def minimise(self, result: Any) -> Any:
    """Caps text and list sizes in tool results, and drops identity fields."""
    if isinstance(result, dict):
      return {k: self.minimise(v) for k, v in result.items() if k.lower() not in IDENTITY_ARG_NAMES}
    if isinstance(result, list):
      return [self.minimise(v) for v in result[: self.policy.max_result_items]]
    if isinstance(result, str) and len(result) > self.policy.max_result_chars:
      return result[: self.policy.max_result_chars] + " ..."
    return result

  # --- ADK hooks
  async def before_tool_callback(self, *, tool, tool_args, tool_context) -> Optional[dict]:
    return self.check(tool.name, dict(tool_args or {}), tool_context.state)

  async def after_tool_callback(self, *, tool, tool_args, tool_context, result) -> Optional[dict]:
    trimmed = self.minimise(result)
    return trimmed if trimmed != result else None
