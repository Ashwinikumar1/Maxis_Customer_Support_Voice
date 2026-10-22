import json

import pytest

from guardrails.tool_policy import (
    K_INJECTION, K_VERIFICATION, K_WRITES_ENABLED, ToolPolicy, ToolPolicyPlugin, ToolRule,
    flag_injection, issue_confirmation, set_verification_level)

BUY = ToolRule("buy_roaming_pass", write=True, args=frozenset({"pass_id", "start_date"}), min_verification=2)
NOW = 1_000_000.0


@pytest.fixture
def plugin():
  return ToolPolicyPlugin(ToolPolicy.default().with_rule(BUY))


def reason(result):
  return result and result["reason"]


# ---------------------------------------------------------------- allow-list and arguments

def test_faq_search_allowed(plugin):
  assert plugin.check("search_knowledge_base", {"query": "roaming in Japan"}, {}) is None


def test_unknown_tool_refused(plugin):
  assert reason(plugin.check("google_search", {"query": "x"}, {})) == "tool_not_allowed"


def test_unexpected_argument_refused(plugin):
  assert reason(plugin.check("search_knowledge_base", {"query": "x", "url": "y"}, {})) == "unexpected_arguments"


def test_long_argument_refused(plugin):
  assert reason(plugin.check("search_knowledge_base", {"query": "x" * 301}, {})) == "argument_too_long"


@pytest.mark.parametrize("value", ["012-345 6789", "+60123456789", "my IC 900101-14-5678", "0198765432"])
def test_identity_values_never_taken_from_the_model(plugin, value):
  assert reason(plugin.check("search_knowledge_base", {"query": value}, {})) == "identity_in_arguments"


def test_rule_cannot_declare_identity_argument():
  with pytest.raises(ValueError):
    ToolRule("lookup", args=frozenset({"msisdn"}))


def test_write_rule_needs_verification_two():
  with pytest.raises(ValueError):
    ToolRule("activate", write=True, min_verification=1)


def test_tool_call_cap(plugin):
  state = {}
  for _ in range(40):
    assert plugin.check("search_knowledge_base", {"query": "x"}, state) is None
  assert reason(plugin.check("search_knowledge_base", {"query": "x"}, state)) == "tool_call_limit"


# ---------------------------------------------------------------- write tools

ARGS = {"pass_id": "JP7", "start_date": "2026-11-01"}


def verified_state():
  state = {}
  set_verification_level(state, 2, source="otp")
  return state


def test_preview_call_without_confirmed_is_allowed(plugin):
  assert plugin.check("buy_roaming_pass", dict(ARGS), {}) is None


def test_confirmed_write_needs_verification(plugin):
  state = {K_VERIFICATION: 1}
  token = issue_confirmation(state, "buy_roaming_pass", ARGS, now=NOW)
  result = plugin.check("buy_roaming_pass", {**ARGS, "confirmed": True, "confirmation_token": token}, state, now=NOW)
  assert reason(result) == "verification_required" and result["next_step"] == "verify_caller"


def test_confirmed_write_with_valid_token_runs_once(plugin):
  state = verified_state()
  token = issue_confirmation(state, "buy_roaming_pass", ARGS, now=NOW)
  call = {**ARGS, "confirmed": True, "confirmation_token": token}
  assert plugin.check("buy_roaming_pass", call, state, now=NOW + 5) is None
  # the same token again: consumed (and the one-write limit is reached anyway)
  assert reason(plugin.check("buy_roaming_pass", call, state, now=NOW + 6)) in ("write_limit", "confirmation_missing")


def test_token_bound_to_arguments(plugin):
  state = verified_state()
  token = issue_confirmation(state, "buy_roaming_pass", ARGS, now=NOW)
  changed = {**ARGS, "pass_id": "WORLD30", "confirmed": True, "confirmation_token": token}
  assert reason(plugin.check("buy_roaming_pass", changed, state, now=NOW)) == "confirmation_mismatch"


def test_token_expires(plugin):
  state = verified_state()
  token = issue_confirmation(state, "buy_roaming_pass", ARGS, now=NOW)
  call = {**ARGS, "confirmed": True, "confirmation_token": token}
  assert reason(plugin.check("buy_roaming_pass", call, state, now=NOW + 121)) == "confirmation_expired"


def test_model_cannot_invent_a_token(plugin):
  state = verified_state()
  call = {**ARGS, "confirmed": True, "confirmation_token": "made-up"}
  assert reason(plugin.check("buy_roaming_pass", call, state, now=NOW)) == "confirmation_missing"


def test_injection_flag_blocks_writes_not_reads(plugin):
  state = verified_state()
  flag_injection(state, source="test")
  token = issue_confirmation(state, "buy_roaming_pass", ARGS, now=NOW)
  call = {**ARGS, "confirmed": True, "confirmation_token": token}
  assert reason(plugin.check("buy_roaming_pass", call, state, now=NOW)) == "injection_flag"
  assert plugin.check("search_knowledge_base", {"query": "roaming"}, state) is None


def test_kill_flags(plugin, monkeypatch):
  state = verified_state()
  token = issue_confirmation(state, "buy_roaming_pass", ARGS, now=NOW)
  call = {**ARGS, "confirmed": True, "confirmation_token": token}
  monkeypatch.setenv("WRITE_TOOLS_ENABLED", "false")
  assert reason(plugin.check("buy_roaming_pass", call, state, now=NOW)) == "writes_disabled"
  monkeypatch.setenv("WRITE_TOOLS_ENABLED", "true")
  state[K_WRITES_ENABLED] = False  # per-call flag, e.g. from Genesys
  assert reason(plugin.check("buy_roaming_pass", call, state, now=NOW)) == "writes_disabled"


def test_verification_level_only_goes_up():
  state = {}
  set_verification_level(state, 2, source="otp")
  set_verification_level(state, 1, source="ivr")
  assert state[K_VERIFICATION] == 2
  with pytest.raises(ValueError):
    set_verification_level(state, 3, source="model")


# ---------------------------------------------------------------- results and events

def test_results_trimmed_and_identity_fields_dropped(plugin):
  result = {"results": ["a" * 5000] + ["b"] * 20, "msisdn": "0123456789"}
  out = plugin.minimise(result)
  assert "msisdn" not in out
  assert len(out["results"]) == 10 and len(out["results"][0]) < 1600


def test_refusals_log_events_without_arguments(plugin, capsys):
  plugin.check("search_knowledge_base", {"query": "call 0123456789"}, {})
  lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
  event = next(e for e in lines if e["security_event"] == "policy_denied")
  assert event["reason"] == "identity_in_arguments"
  assert "0123456789" not in json.dumps(lines)


def test_injection_flag_state_key():
  state = {}
  flag_injection(state, source="heuristic", category="mode_switch")
  assert state[K_INJECTION] is True
