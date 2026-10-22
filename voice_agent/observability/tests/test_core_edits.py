"""Tests for the observability edits made inside existing files.

#5  streaming_core/live_session.py  time-to-first-audio only for user-triggered turns
#6  server.py                       span link to the Cloud Run request trace
#8a streaming_core/gcs_logger.py    GCS upload failure logged with the full exception
#10 streaming_core/live_session.py  Live API session resumption updates on the call span
    observability/logs.py           resumption handle redacted from ADK's log line
#12 streaming_core/live_session.py  per-call token totals from the Live API running totals

(#3b, #8a trace_id and #9c are covered in test_units.py and test_launcher.py.)
Run from voice_agent/:  uv run python -m unittest discover -s observability/tests -t .
"""

import asyncio
import logging
import os
import tempfile
import time
import unittest
from unittest import mock

from google.adk.events import Event
from google.genai import types
from opentelemetry.trace import TraceFlags
from websockets.datastructures import Headers

# Shares the SDK tracer/meter providers set up once in test_units.
from observability.tests.test_units import EXPORTER, TRACER, _metric_points  # noqa: F401
from observability import logs
from server import _request_trace_links
from streaming_core.gcs_logger import TranscriptLogger
from streaming_core.live_session import LiveVoiceSession, _TurnTracker

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def _ttfa_points():
  return _metric_points("voice.time_to_first_audio")


class TimeToFirstAudioTests(unittest.TestCase):  # 5

  def test_model_initiated_turn_is_not_recorded(self):
    before = sum(p.count for p in _ttfa_points())
    turn = _TurnTracker("s1")
    turn.first_audio("maxis_agent")  # greeting: the turn starts AT the first audio
    turn.end("idle")
    self.assertEqual(sum(p.count for p in _ttfa_points()), before, "~0 ms sample must not be recorded")
    span = EXPORTER.get_finished_spans()[-1]
    self.assertEqual(span.attributes["voice.turn_trigger"], "model")
    self.assertNotIn("voice.time_to_first_audio_ms", span.attributes)

  def test_user_triggered_turns_are_recorded_with_trigger(self):
    for trigger in ("user_speech", "user_text"):
      turn = _TurnTracker("s1")
      turn.start("maxis_agent", trigger=trigger)
      time.sleep(0.02)
      turn.first_audio("maxis_agent")
      turn.end("idle")
      span = EXPORTER.get_finished_spans()[-1]
      self.assertEqual(span.attributes["voice.turn_trigger"], trigger)
      self.assertGreaterEqual(span.attributes["voice.time_to_first_audio_ms"], 20)
    triggers = {p.attributes.get("trigger") for p in _ttfa_points()}
    self.assertTrue({"user_speech", "user_text"} <= triggers)
    self.assertNotIn(None, triggers, "every recorded sample carries its trigger")

  def test_send_text_starts_a_user_text_turn(self):
    session = LiveVoiceSession(runner=None, user_id="u", session_id="s", run_config=None,
                               transcript=TranscriptLogger("s"))
    session.send_text("hello")
    self.assertEqual(session.turn.trigger, "user_text")
    session.turn.end("idle")


class RequestTraceLinkTests(unittest.TestCase):  # 6

  def test_traceparent(self):
    (link,) = _request_trace_links(Headers({"traceparent": f"00-{TRACE}-00f067aa0ba902b7-01"}))
    self.assertEqual(f"{link.context.trace_id:032x}", TRACE)
    self.assertEqual(link.context.span_id, 0x00f067aa0ba902b7)
    self.assertEqual(link.attributes["link.source"], "cloud_run_request")

  def test_legacy_header_with_decimal_span_id(self):
    (link,) = _request_trace_links(Headers({"X-Cloud-Trace-Context": f"{TRACE}/12345;o=1"}))
    self.assertEqual(f"{link.context.trace_id:032x}", TRACE)
    self.assertEqual(link.context.span_id, 12345)  # decimal in this header, per Google's docs
    self.assertEqual(link.context.trace_flags, TraceFlags.SAMPLED)

  def test_traceparent_wins_over_legacy_header(self):
    other = "0" * 31 + "1"
    (link,) = _request_trace_links(Headers({
        "traceparent": f"00-{TRACE}-00f067aa0ba902b7-01", "X-Cloud-Trace-Context": f"{other}/1;o=1"}))
    self.assertEqual(f"{link.context.trace_id:032x}", TRACE)

  def test_missing_or_malformed_headers_give_no_link(self):
    self.assertEqual(_request_trace_links(Headers()), [])
    self.assertEqual(_request_trace_links(Headers({"traceparent": "garbage"})), [])
    self.assertEqual(_request_trace_links(Headers({"X-Cloud-Trace-Context": "garbage"})), [])
    self.assertEqual(_request_trace_links(Headers({"X-Cloud-Trace-Context": f"{'0' * 32}/0;o=1"})), [])


class TranscriptUploadFailureTests(unittest.TestCase):  # 8a

  def test_gcs_failure_logged_with_exception(self):
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
      os.chdir(tmp)  # the local fallback writes ./transcripts/
      try:
        with mock.patch.dict(os.environ, {"GCS_BUCKET": "some-bucket"}):
          transcript = TranscriptLogger("session_x", trace_id=TRACE)
        with mock.patch.object(TranscriptLogger, "_upload_gcs", side_effect=PermissionError("403 denied")):
          with self.assertLogs("voice_agent", level="ERROR") as captured:
            asyncio.run(transcript.save())
      finally:
        os.chdir(cwd)
    record = captured.records[0]
    self.assertIn("PermissionError", record.getMessage())
    self.assertIsNotNone(record.exc_info, "full exception + traceback, not just the type name")
    self.assertEqual(transcript.data["metadata"]["trace_id"], TRACE)


def _usage(prompt, output, audio_in=None, audio_out=None, total=None):
  """A Live API usage report as ADK hands it to us (a running total)."""
  return types.GenerateContentResponseUsageMetadata(
      prompt_token_count=prompt, candidates_token_count=output,
      total_token_count=prompt + output if total is None else total,
      prompt_tokens_details=[types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=audio_in)]
      if audio_in is not None else None,
      candidates_tokens_details=[types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=audio_out)]
      if audio_out is not None else None)


class CallTokenUsageTests(unittest.TestCase):  # 12

  def test_running_totals_are_not_added_up(self):
    from streaming_core.live_session import _CallTokenUsage

    tokens = _CallTokenUsage()
    for prompt, output in ((100, 20), (250, 45), (400, 70)):  # "tokens used by the session so far"
      tokens.update(_usage(prompt, output))
    totals = tokens.totals()
    self.assertEqual((totals["input"], totals["output"], totals["total"]), (400, 70, 470))  # latest, not 750/135
    self.assertEqual(tokens.reports, 3)
    self.assertEqual(tokens.counter_resets, 0)

  def test_counter_restart_is_carried_forward(self):
    from streaming_core.live_session import _CallTokenUsage

    tokens = _CallTokenUsage()
    tokens.update(_usage(400, 70))   # first connection ends at 470
    tokens.update(_usage(50, 10))    # new connection: running total starts again
    tokens.update(_usage(120, 30))
    totals = tokens.totals()
    self.assertEqual((totals["input"], totals["output"], totals["total"]), (520, 100, 620))
    self.assertEqual(tokens.counter_resets, 1)

  def test_modality_breakdown_and_missing_total(self):
    from streaming_core.live_session import _CallTokenUsage

    tokens = _CallTokenUsage()
    tokens.update(_usage(300, 80, audio_in=260, audio_out=80, total=0))
    totals = tokens.totals()
    self.assertEqual(totals["input.AUDIO"], 260)
    self.assertEqual(totals["output.AUDIO"], 80)
    self.assertEqual(totals["total"], 380)  # rebuilt from its parts when the report omits it

  def test_each_turn_shows_only_its_own_tokens(self):
    session = LiveVoiceSession(runner=None, user_id="u", session_id="s", run_config=None,
                               transcript=TranscriptLogger("s"))

    async def emit(*_, **__):
      pass

    EXPORTER.clear()
    for prompt, output in ((100, 20), (250, 45)):  # two turns, running totals
      session.turn.start("maxis_agent", trigger="user_speech")
      asyncio.run(session._handle(Event(author="maxis_agent", usage_metadata=_usage(prompt, output)),
                                  emit, session.turn, "maxis_agent"))
      session.turn.end("idle")
    first, second = [s for s in EXPORTER.get_finished_spans() if s.name == "model_turn"][-2:]
    self.assertEqual((first.attributes["gen_ai.usage.input_tokens"], first.attributes["gen_ai.usage.output_tokens"]),
                     (100, 20))
    self.assertEqual((second.attributes["gen_ai.usage.input_tokens"], second.attributes["gen_ai.usage.output_tokens"]),
                     (150, 25))  # the increase, not the running total 250 / 45

  def test_record_token_usage_once_and_empty_without_reports(self):
    session = LiveVoiceSession(runner=None, user_id="u", session_id="s", run_config=None,
                               transcript=TranscriptLogger("s"))
    self.assertEqual(session.record_token_usage(), {})  # no report: nothing recorded, no zeros
    before = sum(p.count for p in _metric_points("voice.call.tokens"))
    session.tokens.update(_usage(400, 70))
    totals = session.record_token_usage()
    session.record_token_usage()  # a second call must not record again
    self.assertEqual((totals["input"], totals["output"], totals["total"], totals["reports"]), (400, 70, 470, 1))
    self.assertEqual(sum(p.count for p in _metric_points("voice.call.tokens")) - before, 3)  # input, output, total

  def test_transcript_metadata(self):
    transcript = TranscriptLogger("s")
    transcript.set_metadata("token_usage", {"input": 400})
    transcript.set_metadata("ignored", None)
    self.assertEqual(transcript.data["metadata"]["token_usage"], {"input": 400})
    self.assertNotIn("ignored", transcript.data["metadata"])


class LiveApiResumptionTests(unittest.TestCase):  # 10

  def test_resumption_updates_counted_on_call_span_without_handle(self):
    session = LiveVoiceSession(runner=None, user_id="u", session_id="s", run_config=None,
                               transcript=TranscriptLogger("s"))

    async def emit(*_, **__):
      pass

    update = types.LiveServerSessionResumptionUpdate(new_handle="SECRET-HANDLE", resumable=True)
    EXPORTER.clear()
    with TRACER.start_as_current_span("voice_session"):
      for _ in range(2):
        asyncio.run(session._handle(Event(author="maxis_agent", live_session_resumption_update=update),
                                    emit, session.turn, "maxis_agent"))
    span = EXPORTER.get_finished_spans()[-1]
    self.assertEqual(span.attributes["live_api.resumption_updates"], 2)
    self.assertIs(span.attributes["live_api.resumable"], True)
    self.assertNotIn("SECRET-HANDLE", repr(dict(span.attributes)))

  def test_handle_redacted_without_the_add_on(self):
    """Plain `python server.py` path: only streaming_core is imported, no add-on.

    Run in a separate process so nothing from the add-on (already imported by
    this test run) can be what does the redaction.
    """
    import subprocess
    import sys
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[2]
    script = (
        "import logging, sys\n"
        "import streaming_core.utils  # what server.py imports on every path\n"
        "assert 'observability' not in sys.modules, 'add-on must not be loaded'\n"
        "from google.genai import types\n"
        "u = types.LiveServerSessionResumptionUpdate(new_handle='SECRET-HANDLE', resumable=True)\n"
        "logging.getLogger('google_adk.google.adk.live._live_llm_flow')"
        ".info(f'Update session resumption handle: {u}.')\n"
    )
    # cwd=app_dir puts the project on sys.path for `-c`; a minimal env keeps the
    # parent's settings (and any add-on configuration) out of the child.
    out = subprocess.run([sys.executable, "-c", script], cwd=app_dir, capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"})
    self.assertEqual(out.returncode, 0, out.stderr)
    self.assertNotIn("SECRET-HANDLE", out.stderr)
    self.assertIn("new_handle='[redacted]'", out.stderr)

  def test_only_one_redaction_filter_with_the_add_on(self):
    import streaming_core.utils  # noqa: F401  (installs its filter)

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
      logs.configure_logging()  # the add-on must see the marker and not add a second one
      adk_logger = logging.getLogger(logs._ADK_LIVE_LOGGER)
      redactors = [f for f in adk_logger.filters if getattr(f, "redacts_resumption_handle", False)]
      self.assertEqual(len(redactors), 1)
    finally:
      root.handlers[:] = saved_handlers
      root.setLevel(saved_level)

  def test_adk_log_line_has_handle_redacted(self):
    adk_logger = logging.getLogger(logs._ADK_LIVE_LOGGER)
    redact = logs.RedactResumptionHandle()
    adk_logger.addFilter(redact)
    try:
      update = types.LiveServerSessionResumptionUpdate(new_handle="SECRET-HANDLE", resumable=True)
      with self.assertLogs(logs._ADK_LIVE_LOGGER, level="INFO") as captured:
        adk_logger.info(f"Update session resumption handle: {update}.")  # ADK's exact wording
      self.assertNotIn("SECRET-HANDLE", captured.output[0])
      self.assertIn("new_handle='[redacted]'", captured.output[0])
      self.assertIn("resumable=True", captured.output[0])
    finally:
      adk_logger.removeFilter(redact)


if __name__ == "__main__":
  unittest.main()
