"""Unit tests for the observability add-on.

Run from voice_agent/:  uv run python -m unittest discover -s observability/tests -t .
"""

import io
import json
import logging
import os
import unittest
from unittest import mock

from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from observability import faq_tracing, logs, spans

# One SDK tracer/meter provider for the whole test module (OTel allows setting them once).
EXPORTER = InMemorySpanExporter()
METRICS = InMemoryMetricReader()
_provider = TracerProvider()
_provider.add_span_processor(spans.SessionSpanProcessor())
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)
from opentelemetry import metrics  # noqa: E402

metrics.set_meter_provider(MeterProvider(metric_readers=[METRICS]))
TRACER = trace.get_tracer("test")


def _metric_points(name):
  data = METRICS.get_metrics_data()
  points = []
  for rm in (data.resource_metrics if data else []):
    for sm in rm.scope_metrics:
      for m in sm.metrics:
        if m.name == name:
          points.extend(m.data.data_points)
  return points


class LoggingTests(unittest.TestCase):

  def _logger(self, formatter):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    log = logging.getLogger(f"test.{id(stream)}")
    log.propagate = False
    log.handlers = [handler, logs.SpanErrorHandler()]
    log.setLevel(logging.DEBUG)
    return log, stream

  def test_json_has_cloud_logging_fields_and_trace(self):
    log, stream = self._logger(logs.CloudJsonFormatter())
    with mock.patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "proj-x"}):
      with TRACER.start_as_current_span("voice_session") as span:
        span.set_attribute("session.id", "session_abc")
        log.warning("hello %s", "world")
    entry = json.loads(stream.getvalue())
    ctx = span.get_span_context()
    self.assertEqual(entry["severity"], "WARNING")
    self.assertEqual(entry["message"], "hello world")
    self.assertEqual(entry["logging.googleapis.com/trace"], f"projects/proj-x/traces/{ctx.trace_id:032x}")
    self.assertEqual(entry["logging.googleapis.com/spanId"], f"{ctx.span_id:016x}")
    self.assertTrue(entry["logging.googleapis.com/trace_sampled"])
    self.assertEqual(entry["session_id"], "session_abc")
    self.assertIn("line", entry["logging.googleapis.com/sourceLocation"])

  def test_json_without_span_has_no_trace(self):
    log, stream = self._logger(logs.CloudJsonFormatter())
    log.info("plain")
    entry = json.loads(stream.getvalue())
    self.assertNotIn("logging.googleapis.com/trace", entry)
    self.assertEqual(entry["severity"], "INFO")

  def test_exception_is_one_entry_for_error_reporting(self):
    log, stream = self._logger(logs.CloudJsonFormatter())
    try:
      raise ValueError("boom")
    except ValueError:
      log.exception("Session %s failed", "s1")
    lines = stream.getvalue().strip().splitlines()
    self.assertEqual(len(lines), 1)  # traceback is escaped inside one JSON line
    entry = json.loads(lines[0])
    self.assertEqual(entry["severity"], "ERROR")
    self.assertIn("Traceback", entry["message"])
    self.assertIn("ReportedErrorEvent", entry["@type"])

  def test_error_log_marks_current_span_error(self):
    log, _ = self._logger(logs.TraceTextFormatter())
    EXPORTER.clear()
    with TRACER.start_as_current_span("voice_session"):
      try:
        raise RuntimeError("live api down")
      except RuntimeError:
        log.exception("Session failed")
    span = EXPORTER.get_finished_spans()[-1]
    self.assertEqual(span.status.status_code, StatusCode.ERROR)
    self.assertTrue(any(e.name == "exception" for e in span.events))

  def test_warning_does_not_mark_span_error(self):
    log, _ = self._logger(logs.TraceTextFormatter())
    EXPORTER.clear()
    with TRACER.start_as_current_span("voice_session"):
      log.warning("just a warning")
    self.assertNotEqual(EXPORTER.get_finished_spans()[-1].status.status_code, StatusCode.ERROR)


class SessionSpanTests(unittest.TestCase):

  def test_children_inherit_session_id_and_session_metrics_recorded(self):
    EXPORTER.clear()
    with TRACER.start_as_current_span("voice_session") as root:
      root.set_attribute("session.id", "session_42")
      with TRACER.start_as_current_span("invoke_agent"):
        with TRACER.start_as_current_span("execute_tool search_knowledge_base"):
          pass
    by_name = {s.name: s for s in EXPORTER.get_finished_spans()}
    for name in ("invoke_agent", "execute_tool search_knowledge_base"):
      self.assertEqual(by_name[name].attributes["session.id"], "session_42")
      self.assertEqual(by_name[name].attributes["gen_ai.conversation.id"], "session_42")
    self.assertTrue(_metric_points("voice.session.duration"))
    finished = _metric_points("voice.sessions")
    self.assertTrue(any(p.attributes.get("outcome") == "ok" for p in finished))
    active = _metric_points("voice.sessions.active")
    self.assertEqual(sum(p.value for p in active), 0)  # started and ended

  def test_failed_session_counted_as_error(self):
    with TRACER.start_as_current_span("voice_session") as root:
      root.set_status(trace.Status(StatusCode.ERROR))
    finished = _metric_points("voice.sessions")
    self.assertTrue(any(p.attributes.get("outcome") == "error" for p in finished))


class FaqTracingTests(unittest.TestCase):

  def test_success_span(self):
    EXPORTER.clear()
    traced = faq_tracing.wrap(lambda q: ["a", "b"], "engine-1")
    self.assertEqual(traced("roaming"), ["a", "b"])
    span = EXPORTER.get_finished_spans()[-1]
    self.assertEqual(span.name, "vertex_ai_search.search")
    self.assertEqual(span.attributes["vertex_ai_search.result_count"], 2)
    self.assertEqual(span.attributes["vertex_ai_search.query_length"], 7)
    self.assertNotIn("roaming", json.dumps(dict(span.attributes)))  # query text never recorded

  def test_failure_is_logged_and_marked(self):
    EXPORTER.clear()

    class FakeHTTPError(Exception):
      response = type("R", (), {"status_code": 403})()

    def failing(_):
      raise FakeHTTPError("forbidden")

    traced = faq_tracing.wrap(failing, "engine-1")
    with self.assertLogs("voice_agent.faq_search", level="ERROR") as captured:
      with self.assertRaises(FakeHTTPError):
        traced("q")
    self.assertIn("HTTP 403", captured.output[0])
    span = EXPORTER.get_finished_spans()[-1]
    self.assertEqual(span.status.status_code, StatusCode.ERROR)
    self.assertEqual(span.attributes["http.response.status_code"], 403)
    outcomes = {p.attributes.get("outcome") for p in _metric_points("voice.faq_search.duration")}
    self.assertIn("FakeHTTPError", outcomes)

  def test_tool_still_returns_friendly_error(self):
    import asyncio

    from voice_assistant import faq_search_tool as tool

    original = tool._search
    try:
      tool._search = faq_tracing.wrap(mock.Mock(side_effect=KeyError("GOOGLE_CLOUD_PROJECT")), "e")
      with self.assertLogs("voice_agent.faq_search", level="ERROR"):
        result = asyncio.run(tool.search_knowledge_base("plans"))
      self.assertIn("error", result)  # existing behaviour preserved
    finally:
      tool._search = original

  def test_wrap_is_idempotent(self):
    traced = faq_tracing.wrap(lambda q: [], "e")
    self.assertIs(faq_tracing.wrap(traced, "e"), traced)


if __name__ == "__main__":
  unittest.main()
