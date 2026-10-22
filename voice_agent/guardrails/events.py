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


def security_event(name: str, **fields: Any) -> dict:
  """Writes one security event line and returns it (for tests)."""
  event = build_event(name, **fields)
  sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
  sys.stdout.flush()
  return event
