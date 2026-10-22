"""Tests for the observability edits made inside existing files.

#5  streaming_core/live_session.py  time-to-first-audio only for user-triggered turns
#6  server.py                       span link to the Cloud Run request trace
#8a streaming_core/gcs_logger.py    GCS upload failure logged with the full exception
#10 streaming_core/live_session.py  Live API session resumption updates on the call span
    observability/logs.py           resumption handle redacted from ADK's log line

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
