"""Transcript redaction before anything is stored (baseline DP-03, DP-08 to DP-11).

RedactingTranscriptLogger is a drop-in replacement for streaming_core.TranscriptLogger. Every
turn and event is redacted as it is recorded, so unredacted speech never reaches Cloud Storage
or the local fallback file.

Two layers:
1. Local redaction, always on and offline: spoken numbers in English and Malay are normalised to
   digits first ("satu dua tiga", "double five"), then MyKad, Malaysian phone numbers, payment
   card numbers (Luhn-checked) and email addresses are replaced with type tags.
2. Sensitive Data Protection, when DLP_DEIDENTIFY_TEMPLATE and DLP_INSPECT_TEMPLATE are set:
   the whole transcript is sent once at save time through the voice-agent templates (names,
   addresses, passports...). If that call fails, the locally redacted transcript is stored and
   a security event is raised; unredacted text is never the fallback.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

from streaming_core.gcs_logger import TranscriptLogger

from .events import security_event

# ----------------------------------------------------------------------------- spoken numbers

_DIGITS = {
    # English
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
    # Malay
    "kosong": "0", "sifar": "0", "satu": "1", "dua": "2", "tiga": "3", "empat": "4", "lima": "5",
    "enam": "6", "tujuh": "7", "lapan": "8", "sembilan": "9",
}
_REPEAT = {"double": 2, "triple": 3}
_NUM_WORD = "|".join(sorted(_DIGITS, key=len, reverse=True))
# A run of at least four digit-words (or digits), optionally with double/triple and separators.
_SPOKEN_RUN = re.compile(
    rf"\b(?:(?:double|triple)\s+)?(?:{_NUM_WORD}|\d)(?:[\s,.-]+(?:(?:double|triple)\s+)?(?:{_NUM_WORD}|\d)){{3,}}\b",
    re.I,
)


def _run_to_digits(run: str) -> str:
  out, repeat = [], 1
  for token in re.split(r"[\s,.-]+", run.lower()):
    if token in _REPEAT:
      repeat = _REPEAT[token]
      continue
    digit = _DIGITS.get(token, token if token.isdigit() else "")
    out.append(digit * repeat)
    repeat = 1
  return "".join(out)


def normalise_spoken_digits(text: str) -> str:
  """'my IC is nine zero zero one zero one ...' -> 'my IC is 900101...' (only runs of 4+)."""
  return _SPOKEN_RUN.sub(lambda m: _run_to_digits(m.group(0)), text)


# ----------------------------------------------------------------------------- detectors

_MYKAD = re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[-\s]?\d{2}[-\s]?\d{4}(?!\d)")
_PHONE = re.compile(r"(?<!\d)(?:\+?60[-\s]?|0)\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[\s-]?){13,19}(?!\d)")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def _luhn_ok(number: str) -> bool:
  digits = [int(d) for d in re.sub(r"\D", "", number)]
  if not 13 <= len(digits) <= 19:
    return False
  total = 0
  for i, d in enumerate(reversed(digits)):
    if i % 2:
      d *= 2
      d -= 9 if d > 9 else 0
    total += d
  return total % 10 == 0


def redact_text(text: str) -> str:
  """Local, offline redaction. Order matters: cards before phones, MyKad before phones."""
  if not text:
    return text
  text = normalise_spoken_digits(text)
  text = _EMAIL.sub("[EMAIL]", text)
  text = _CARD.sub(lambda m: "[CARD_NUMBER]" if _luhn_ok(m.group(0)) else m.group(0), text)
  text = _MYKAD.sub("[MYKAD_NUMBER]", text)
  text = _PHONE.sub("[PHONE_NUMBER]", text)
  return text


def _redact_value(value):
  if isinstance(value, str):
    return redact_text(value)
  if isinstance(value, dict):
    return {k: _redact_value(v) for k, v in value.items()}
  if isinstance(value, list):
    return [_redact_value(v) for v in value]
  return value


# ----------------------------------------------------------------------------- Sensitive Data Protection

def _dlp_deidentify(texts: list[str]) -> list[str]:
  """Runs the transcript through the voice-agent inspect and de-identify templates (one call)."""
  import google.auth
  from google.auth.transport.requests import AuthorizedSession

  inspect = os.environ["DLP_INSPECT_TEMPLATE"]
  deidentify = os.environ["DLP_DEIDENTIFY_TEMPLATE"]
  parent = inspect.split("/inspectTemplates/")[0]
  creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
  table = {"headers": [{"name": "text"}], "rows": [{"values": [{"string_value": t}]} for t in texts]}
  resp = AuthorizedSession(creds).post(
      f"https://dlp.googleapis.com/v2/{parent}/content:deidentify", timeout=20,
      json={"inspectTemplateName": inspect, "deidentifyTemplateName": deidentify, "item": {"table": table}})
  resp.raise_for_status()
  rows = resp.json()["item"]["table"]["rows"]
  return [r["values"][0].get("stringValue", "") for r in rows]


class RedactingTranscriptLogger(TranscriptLogger):
  """TranscriptLogger that never holds or stores unredacted text."""

  def add_turn(self, speaker: str, text: str, agent: str | None = None):
    super().add_turn(speaker, redact_text(text), agent=agent)

  def add_event(self, event: str, **details):
    super().add_event(event, **_redact_value(details))

  async def save(self):
    if os.getenv("DLP_DEIDENTIFY_TEMPLATE") and os.getenv("DLP_INSPECT_TEMPLATE"):
      turns = [t for t in self.data["conversation"] if isinstance(t.get("text"), str) and t["text"]]
      if turns:
        try:
          cleaned = await asyncio.to_thread(_dlp_deidentify, [t["text"] for t in turns])
          for turn, text in zip(turns, cleaned):
            turn["text"] = text
          self.data["metadata"]["redaction"] = "local+dlp"
        except Exception as exc:  # local redaction already applied; record the gap
          self.data["metadata"]["redaction"] = "local"
          security_event("dlp_redaction_failed", session=self.data.get("session_id"),
                         detail_code=type(exc).__name__, severity="ERROR")
    else:
      self.data["metadata"].setdefault("redaction", "local")
    await super().save()

  def payload_preview(self) -> str:
    """The JSON that save() would write (for tests)."""
    return json.dumps(self.data, ensure_ascii=False)
