"""Removes personal data from transcripts before they are stored (design section 7).

REDACT_TRANSCRIPTS chooses how:
  none   keep text as spoken (local debugging only);
  regex  (default) mask Malaysian IC numbers, card numbers, phone numbers and
         emails with local patterns: no network call, always available;
  dlp    regex first, then Cloud DLP (Sensitive Data Protection) de-identify,
         replacing findings with their info type, e.g. [PERSON_NAME].
         Needs roles/dlp.user on the runtime service account. If DLP fails,
         the regex result is kept and the transcript records dlp_failed.

Which personal data Maxis wants removed is still open item 1 in the design;
DLP_INFO_TYPES makes the list configurable.
"""

from __future__ import annotations

import os
import re

from .utils import logger

_PATTERNS = [
    # Malaysian IC (MyKad): YYMMDD-PB-###G, with or without dashes.
    ("[IC_NUMBER]", re.compile(r"\b\d{6}[-\s]?\d{2}[-\s]?\d{4}\b")),
    # Card numbers: 13-19 digits, optionally grouped.
    ("[CARD_NUMBER]", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    ("[EMAIL_ADDRESS]", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
]
# Phone numbers: Malaysian (+60 / 60 / 0 prefix), international (+...), and any
# run of 9+ digits. Dates such as 2026-10-26 are deliberately not matched.
_PHONE_PATTERNS = [
    re.compile(r"(?<![\w+])(?:\+?60|0)\d{1,2}[\s-]?\d{3,4}[\s-]?\d{4}(?!\d)"),
    re.compile(r"\+\d[\d\s-]{7,}\d(?!\d)"),
    re.compile(r"(?<!\d)\d{9,}(?!\d)"),
]
_DEFAULT_INFO_TYPES = "PHONE_NUMBER,EMAIL_ADDRESS,CREDIT_CARD_NUMBER,PERSON_NAME,STREET_ADDRESS,PASSPORT"
_session = None


def mode() -> str:
  value = os.getenv("REDACT_TRANSCRIPTS", "regex").strip().lower()
  return value if value in ("none", "regex", "dlp") else "regex"


def mask_phone_numbers(text: str, label: str = "[PHONE_NUMBER]") -> str:
  for pattern in _PHONE_PATTERNS:
    text = pattern.sub(label, text)
  return text


def regex_redact(text: str) -> str:
  for label, pattern in _PATTERNS:
    text = pattern.sub(label, text)
  return mask_phone_numbers(text)


def _dlp_redact(texts: list[str]) -> list[str]:
  """One Cloud DLP de-identify call for all texts (as a one-column table)."""
  global _session
  if _session is None:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    _session = AuthorizedSession(creds)
  project = os.environ["GOOGLE_CLOUD_PROJECT"]
  info_types = [t.strip() for t in os.getenv("DLP_INFO_TYPES", _DEFAULT_INFO_TYPES).split(",") if t.strip()]
  body = {
      "item": {"table": {"headers": [{"name": "text"}],
                         "rows": [{"values": [{"stringValue": t}]} for t in texts]}},
      "inspectConfig": {
          "infoTypes": [{"name": t} for t in info_types],
          "customInfoTypes": [{"infoType": {"name": "MY_IC_NUMBER"},
                               "regex": {"pattern": r"\d{6}-?\d{2}-?\d{4}"}}],
      },
      "deidentifyConfig": {"infoTypeTransformations": {"transformations": [
          {"primitiveTransformation": {"replaceWithInfoTypeConfig": {}}}]}},
  }
  url = f"https://dlp.googleapis.com/v2/projects/{project}/locations/global/content:deidentify"
  resp = _session.post(url, json=body, timeout=15, headers={"X-Goog-User-Project": project})
  resp.raise_for_status()
  rows = resp.json()["item"]["table"]["rows"]
  return [row["values"][0].get("stringValue", "") for row in rows]


def _strings(value):
  """Applies regex_redact to every string inside a JSON-like value."""
  if isinstance(value, str):
    return regex_redact(value)
  if isinstance(value, list):
    return [_strings(v) for v in value]
  if isinstance(value, dict):
    return {k: _strings(v) for k, v in value.items()}
  return value


def redact_transcript(data: dict) -> dict:
  """Redacts the conversation in place (called from a worker thread) and records how."""
  how = mode()
  data["metadata"]["redaction"] = how
  if how == "none":
    return data
  turns = data["conversation"]
  for i, entry in enumerate(turns):
    turns[i] = {k: (v if k in ("speaker", "timestamp", "agent", "event") else _strings(v))
                for k, v in entry.items()}
  if how == "dlp":
    spoken = [t for t in turns if t.get("text")]
    if spoken:
      try:
        for entry, clean in zip(spoken, _dlp_redact([t["text"] for t in spoken])):
          entry["text"] = clean
      except Exception as e:
        logger.error("DLP redaction failed (%s); keeping regex redaction", type(e).__name__)
        data["metadata"]["redaction"] = "regex (dlp_failed)"
  return data
