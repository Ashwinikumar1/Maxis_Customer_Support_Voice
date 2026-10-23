"""Structured security events (baseline AP-18, LM-13).

One JSON line per security-relevant step, written to stdout. On Cloud Run each line becomes a
log entry whose jsonPayload carries `security_event`; the alert policies in
security/terraform/modules/logging-scc/detections.tf match on that field.

Events never carry what the caller said, phone numbers, OTPs or raw tool arguments. Identifiers
are reduced to a keyed hash (HMAC-SHA256 with SECURITY_EVENT_HMAC_KEY), so the same session or
line can be correlated across events without being readable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

# Only these fields may appear in an event; anything else is dropped.
ALLOWED_FIELDS = frozenset({
    "session", "line", "tool", "result", "reason", "verification_level", "source",
    "category", "count", "agent", "detail_code",
})
# Fields whose values are identifiers: always hashed.
HASHED_FIELDS = frozenset({"session", "line"})
_MAX_VALUE_LEN = 120


def _hash(value: str) -> str:
  key = os.getenv("SECURITY_EVENT_HMAC_KEY", "").encode()
  if key:
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:24]
  # Without a key the hash is still one-way, but anyone can recompute it from a known value.
  return "u_" + hashlib.sha256(value.encode()).hexdigest()[:22]


def hash_id(value: str) -> str:
  """The same keyed hash the events use, for ordinary log lines and trace attributes (LM-07).

  Using one function everywhere means a session hash in an application log, a security event,
  a trace and the transcript's metadata all match, so the SOC can join them without anyone
  needing the raw session, account or line ID.
  """
  return _hash(str(value))


def build_event(name: str, **fields: Any) -> dict:
  event: dict[str, Any] = {
      "security_event": name,
      "severity": fields.pop("severity", "NOTICE"),
      "time": datetime.now(timezone.utc).isoformat(),
  }
  for key, value in fields.items():
    if key not in ALLOWED_FIELDS or value is None:
      continue
    if key in HASHED_FIELDS:
      value = _hash(str(value))
    elif isinstance(value, str):
      value = value[:_MAX_VALUE_LEN]
    elif not isinstance(value, (int, float, bool)):
      value = str(value)[:_MAX_VALUE_LEN]
    event[key] = value
  return event


def _trace_link(event: dict) -> dict:
  """Links the event to the call, like every other log line (observability/logs.py).

  - logging.googleapis.com/trace and /spanId: Cloud Logging shows the event under the call's
    trace, so the SOC can open the whole call from an alert;
  - session_id: the call's hashed session_ref (streaming_core.utils.current_session_id), the
    same value as session.id on the call's spans and session_id on its other log lines;
  - a span event "security_event.<name>" with the same allow-listed fields on the active span
    (e.g. a refused tool call shows on its execute_tool span), and for policy_denied the span
    attribute guardrails.refused_reason, so refusals can be filtered in Trace Explorer.
  Never fails the caller: any problem here just leaves the event unlinked.
  """
  try:
    from opentelemetry import trace

    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
      project = os.getenv("GOOGLE_CLOUD_PROJECT", "")
      trace_id = f"{ctx.trace_id:032x}"
      event["logging.googleapis.com/trace"] = f"projects/{project}/traces/{trace_id}" if project else trace_id
      event["logging.googleapis.com/spanId"] = f"{ctx.span_id:016x}"
      event["logging.googleapis.com/trace_sampled"] = ctx.trace_flags.sampled
    if span.is_recording():
      attrs = {k: v for k, v in event.items() if k in ALLOWED_FIELDS}
      span.add_event(f"security_event.{event['security_event']}", attrs)
      if event["security_event"] == "policy_denied" and "reason" in event:
        span.set_attribute("guardrails.refused_reason", event["reason"])
  except Exception:
    pass
  try:
    from streaming_core.utils import current_session_id

    if (session := current_session_id.get()):
      event["session_id"] = session
  except Exception:
    pass
  return event


def security_event(name: str, **fields: Any) -> dict:
  """Writes one security event line and returns it (for tests)."""
  event = _trace_link(build_event(name, **fields))
  sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
  sys.stdout.flush()
  return event
