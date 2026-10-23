import asyncio
import json

import pytest

from guardrails import events
from guardrails.redaction import RedactingTranscriptLogger, normalise_spoken_digits, redact_text


@pytest.mark.parametrize("spoken,digits", [
    ("nine zero zero one zero one one four five six seven eight", "900101145678"),
    ("kosong satu dua tiga empat lima enam tujuh lapan sembilan", "0123456789"),
    ("zero one double two three four five six seven", "012234567"),
    ("oh one two, three four five - six seven eight nine", "0123456789"),
])
def test_spoken_numbers_become_digits(spoken, digits):
  assert normalise_spoken_digits(spoken) == digits


def test_short_number_phrases_are_left_alone():
  assert normalise_spoken_digits("I need one or two passes") == "I need one or two passes"


@pytest.mark.parametrize("text,tag", [
    ("my IC is 900101-14-5678", "[MYKAD_NUMBER]"),
    ("my IC is nine zero zero one zero one one four five six seven eight", "[MYKAD_NUMBER]"),
    ("call me on 012-345 6789", "[PHONE_NUMBER]"),
    ("nombor saya kosong satu dua tiga empat lima enam tujuh lapan sembilan", "[PHONE_NUMBER]"),
    ("+60 12 345 6789", "[PHONE_NUMBER]"),
    ("card 4111 1111 1111 1111", "[CARD_NUMBER]"),
    ("email me at ali@example.com", "[EMAIL]"),
])
def test_personal_data_redacted(text, tag):
  out = redact_text(text)
  assert tag in out
  assert not any(ch.isdigit() for ch in out.split(tag)[0][-6:])


def test_ordinary_numbers_kept():
  text = "The Japan pass is 38 ringgit for 7 days, and 2026-11-01 is fine"
  assert redact_text(text) == text


def test_non_luhn_long_number_not_called_a_card():
  assert "[CARD_NUMBER]" not in redact_text("ref 1234 5678 9012 3456")


def test_transcript_never_holds_raw_pii(tmp_path, monkeypatch):
  monkeypatch.chdir(tmp_path)
  monkeypatch.delenv("GCS_BUCKET", raising=False)
  monkeypatch.delenv("DLP_DEIDENTIFY_TEMPLATE", raising=False)
  log = RedactingTranscriptLogger("session_abc", channel="test")
  log.add_turn("User", "my number is 012 345 6789 and IC 900101-14-5678")
  log.add_event("tool_call", name="search_knowledge_base", args={"query": "card 4111111111111111"})
  preview = log.payload_preview()
  for raw in ("012 345 6789", "900101-14-5678", "4111111111111111"):
    assert raw not in preview
  asyncio.run(log.save())
  saved = next((tmp_path / "transcripts").glob("*.json")).read_text()
  assert "900101" not in saved and json.loads(saved)["metadata"]["redaction"] == "local"


def test_dlp_failure_keeps_local_redaction_and_raises_event(tmp_path, monkeypatch, capsys):
  monkeypatch.chdir(tmp_path)
  monkeypatch.delenv("GCS_BUCKET", raising=False)
  monkeypatch.setenv("DLP_DEIDENTIFY_TEMPLATE", "projects/p/locations/l/deidentifyTemplates/d")
  monkeypatch.setenv("DLP_INSPECT_TEMPLATE", "projects/p/locations/l/inspectTemplates/i")
  import guardrails.redaction as r
  monkeypatch.setattr(r, "_dlp_deidentify", lambda texts: (_ for _ in ()).throw(RuntimeError("down")))
  log = RedactingTranscriptLogger("s1")
  log.add_turn("User", "IC 900101-14-5678")
  asyncio.run(log.save())
  saved = next((tmp_path / "transcripts").glob("*.json")).read_text()
  assert "900101" not in saved
  assert "dlp_redaction_failed" in capsys.readouterr().out


def test_events_hash_identifiers_and_drop_unknown_fields(monkeypatch):
  monkeypatch.setenv("SECURITY_EVENT_HMAC_KEY", "k")
  e = events.build_event("x", session="session_123", line="0123456789", query="secret words", tool="t")
  assert e["session"] != "session_123" and e["line"] != "0123456789"
  assert "query" not in e and e["tool"] == "t"
  assert events.build_event("x", session="session_123")["session"] == e["session"]  # stable


def test_dlp_request_uses_rest_field_names_and_batches(monkeypatch):
  """REST body uses lowerCamelCase (stringValue) and stays under the 0.5 MB request limit."""
  import google.auth
  from google.auth.transport import requests as gat

  from guardrails import redaction as r

  monkeypatch.setenv("DLP_INSPECT_TEMPLATE", "projects/p/locations/asia-southeast1/inspectTemplates/i")
  monkeypatch.setenv("DLP_DEIDENTIFY_TEMPLATE", "projects/p/locations/asia-southeast1/deidentifyTemplates/d")
  monkeypatch.setattr(google.auth, "default", lambda scopes=None: (object(), "p"))
  bodies = []

  class FakeResp:
    def __init__(self, body):
      self.body = body

    def raise_for_status(self):
      pass

    def json(self):
      rows = self.body["item"]["table"]["rows"]
      return {"item": {"table": {"rows": [{"values": [{"stringValue": "[X]"}]} for _ in rows]}}}

  class FakeSession:
    def __init__(self, creds):
      pass

    def post(self, url, timeout, json):
      assert url == "https://dlp.googleapis.com/v2/projects/p/locations/asia-southeast1/content:deidentify"
      bodies.append(json)
      return FakeResp(json)

  monkeypatch.setattr(gat, "AuthorizedSession", FakeSession)
  texts = ["x" * 100_000 for _ in range(10)]  # ~1 MB in total
  out = r._dlp_deidentify(texts)
  assert out == ["[X]"] * 10
  assert len(bodies) >= 3
  assert all("stringValue" in b["item"]["table"]["rows"][0]["values"][0] for b in bodies)
  assert all(len(__import__("json").dumps(b)) < 500_000 for b in bodies)
