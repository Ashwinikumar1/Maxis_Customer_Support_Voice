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

from observability import faq_tracing, gateway_tracing, logs, spans

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

  def test_unsampled_call_keeps_session_id_in_logs(self):
    """[#13] An unsampled call's span has no attributes; the session ID must come
    from streaming_core.utils.current_session_id instead."""
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, use_span

    from streaming_core.utils import current_session_id

    log, stream = self._logger(logs.CloudJsonFormatter())
    unsampled = NonRecordingSpan(SpanContext(trace_id=0xABC, span_id=0x123, is_remote=False,
                                             trace_flags=TraceFlags(TraceFlags.DEFAULT)))
    token = current_session_id.set("session_unsampled")
    try:
      with use_span(unsampled):
        log.info("inside an unsampled call")
    finally:
      current_session_id.reset(token)
    entry = json.loads(stream.getvalue())
    self.assertEqual(entry["session_id"], "session_unsampled")
    self.assertIs(entry["logging.googleapis.com/trace_sampled"], False)

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

  def test_children_inherit_session_id(self):
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
    # [#13] The call metrics moved to server.py (sampling-proof); see
    # test_launcher.test_sampling_off_keeps_metrics_and_log_session_ids.


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

  def test_failure_is_marked_and_raised_without_logging(self):
    EXPORTER.clear()

    class FakeHTTPError(Exception):
      response = type("R", (), {"status_code": 403})()

    def failing(_):
      raise FakeHTTPError("forbidden")

    traced = faq_tracing.wrap(failing, "engine-1")
    # The tool's own `except` logs the failure; the wrapper must not log it twice.
    with self.assertNoLogs("voice_agent.faq_search", level="ERROR"):
      with self.assertRaises(FakeHTTPError):
        traced("q")
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
      with self.assertLogs("voice_agent.faq_search", level="ERROR") as captured:
        result = asyncio.run(tool.search_knowledge_base("plans"))
      self.assertIn("error", result)  # existing behaviour preserved
      self.assertEqual(len(captured.records), 1, "each failure is logged exactly once")
      self.assertIn("KeyError", captured.records[0].getMessage())
      self.assertIsNotNone(captured.records[0].exc_info)
      self.assertNotIn("plans", captured.output[0])  # the query (PII) is not logged
    finally:
      tool._search = original

  def test_wrap_is_idempotent(self):
    traced = faq_tracing.wrap(lambda q: [], "e")
    self.assertIs(faq_tracing.wrap(traced, "e"), traced)


class GatewayTracingTests(unittest.TestCase):
  """The roaming tools' gateway calls get the same coverage as the FAQ search (gap #3)."""

  HEADERS = {"X-Caller-Id": "60100000001", "X-Conversation-Id": "session_x"}

  @staticmethod
  def _send(response=None, exc=None):
    async def send(method, path, **kwargs):
      if exc:
        raise exc
      return response
    return send

  def _call(self, send, method="GET", path="/roaming/passes"):
    import asyncio

    traced = gateway_tracing.wrap(send)
    return asyncio.run(traced(method, path, headers=self.HEADERS, params={"destinations": "Tokyo"}, body=None))

  def test_success_span_has_operation_and_next_step(self):
    import httpx

    EXPORTER.clear()
    resp = httpx.Response(200, json={"status": "success", "responseData": {"next_step": "recommend_pass"}})
    self._call(self._send(resp))
    span = EXPORTER.get_finished_spans()[-1]
    self.assertEqual(span.name, "agent_gateway.request")
    self.assertEqual(span.attributes["agent_gateway.operation"], "findPasses")
    self.assertEqual(span.attributes["agent_gateway.next_step"], "recommend_pass")
    self.assertEqual(span.attributes["http.response.status_code"], 200)
    self.assertNotEqual(span.status.status_code, StatusCode.ERROR)
    recorded = json.dumps(dict(span.attributes))
    self.assertNotIn("60100000001", recorded)  # the caller's number is never recorded
    self.assertNotIn("Tokyo", recorded)        # nor the arguments

  def test_business_refusal_is_not_an_error(self):
    import httpx

    EXPORTER.clear()
    resp = httpx.Response(200, json={"status": "fail", "violations": [
        {"code": "NOT_A_MAXIS_NUMBER", "action": "general_help_only"}]})
    self._call(self._send(resp), path="/roaming/lines")
    span = EXPORTER.get_finished_spans()[-1]
    self.assertEqual(span.attributes["agent_gateway.violation_code"], "NOT_A_MAXIS_NUMBER")
    self.assertNotEqual(span.status.status_code, StatusCode.ERROR)

  def test_backend_unavailable_and_5xx_are_errors(self):
    import httpx

    EXPORTER.clear()
    self._call(self._send(httpx.Response(200, json={"status": "fail", "violations": [
        {"code": "BACKEND_UNAVAILABLE", "action": "apologise_and_offer_agent"}]})), "POST", "/roaming/purchases")
    self._call(self._send(httpx.Response(503, text="down")), "POST", "/roaming/switch")
    unavailable, http_503 = EXPORTER.get_finished_spans()[-2:]
    self.assertEqual(unavailable.status.status_code, StatusCode.ERROR)
    self.assertEqual(unavailable.attributes["agent_gateway.operation"], "buyPass")
    self.assertEqual(http_503.status.status_code, StatusCode.ERROR)
    self.assertEqual(http_503.attributes["http.response.status_code"], 503)

  def test_network_failure_is_marked_raised_and_measured(self):
    import httpx

    EXPORTER.clear()
    with self.assertRaises(httpx.ConnectError):
      self._call(self._send(exc=httpx.ConnectError("down")))
    span = EXPORTER.get_finished_spans()[-1]
    self.assertEqual(span.status.status_code, StatusCode.ERROR)
    self.assertTrue(any(e.name == "exception" for e in span.events))
    points = _metric_points("voice.gateway.duration")
    self.assertIn(("findPasses", "ConnectError"),
                  {(p.attributes.get("operation"), p.attributes.get("outcome")) for p in points})

  def test_tool_logs_failure_once_and_marks_tool_span(self):
    import asyncio
    from types import SimpleNamespace

    import httpx

    from voice_assistant import gateway_client, roaming_tools

    original = gateway_client._send
    EXPORTER.clear()
    try:
      gateway_client._send = gateway_tracing.wrap(self._send(exc=httpx.ConnectError("down")))
      ctx = SimpleNamespace(state={"caller_id": "60100000001"}, session=SimpleNamespace(id="s"))
      with TRACER.start_as_current_span("execute_tool findPasses"):
        with self.assertLogs("voice_agent", level="ERROR") as captured:
          result = asyncio.run(roaming_tools.findPasses("Tokyo", ctx))
      self.assertEqual(result["violations"][0]["code"], "BACKEND_UNAVAILABLE")  # polite answer kept
      self.assertEqual(len(captured.records), 1, "each failure is logged exactly once")
      self.assertIsNotNone(captured.records[0].exc_info)
      self.assertNotIn("60100000001", captured.output[0])
      self.assertNotIn("Tokyo", captured.output[0])
      names = [s.name for s in EXPORTER.get_finished_spans()]
      self.assertEqual(names[-2:], ["agent_gateway.request", "execute_tool findPasses"])
    finally:
      gateway_client._send = original

  def test_wrap_is_idempotent(self):
    traced = gateway_tracing.wrap(self._send())
    self.assertIs(gateway_tracing.wrap(traced), traced)


class SecurityEventLinkTests(unittest.TestCase):
  """Security events are linked to the call like every other log line."""

  def test_event_carries_trace_session_and_span_event(self):
    from contextlib import redirect_stdout

    from guardrails.events import security_event
    from streaming_core.utils import current_session_id

    EXPORTER.clear()
    token = current_session_id.set("u_abc")
    out = io.StringIO()
    try:
      with TRACER.start_as_current_span("execute_tool buyPass") as span:
        with redirect_stdout(out):
          security_event("policy_denied", tool="buyPass", reason="verification_required", severity="WARNING")
        trace_hex = f"{span.get_span_context().trace_id:032x}"
    finally:
      current_session_id.reset(token)
    line = json.loads(out.getvalue())
    self.assertEqual(line["security_event"], "policy_denied")
    self.assertTrue(line["logging.googleapis.com/trace"].endswith(trace_hex))
    self.assertEqual(line["session_id"], "u_abc")
    finished = EXPORTER.get_finished_spans()[-1]
    self.assertEqual(finished.attributes["guardrails.refused_reason"], "verification_required")
    self.assertIn("security_event.policy_denied", [e.name for e in finished.events])


if __name__ == "__main__":
  unittest.main()
