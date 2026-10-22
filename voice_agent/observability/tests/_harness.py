"""Test harness: the real launcher, with spans written to a local JSONL file.

Simulates what ADK does on Cloud Run (maybe_set_otel_providers with an
exporter) but exports to OBS_TEST_SPANS_FILE instead of Cloud Trace, and -
when OBS_TEST_METRICS_FILE is set - writes the final metric values there on exit.
"""

import atexit
import json
import os

from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from observability import __main__ as launcher
from observability.bootstrap import install as real_install


class _FileExporter(SpanExporter):

  def __init__(self, path):
    self.path = path

  def export(self, spans):
    with open(self.path, "a", encoding="utf-8") as fh:
      for s in spans:
        fh.write(json.dumps({"name": s.name, "status": s.status.status_code.name,
                             "trace_id": f"{s.context.trace_id:032x}",
                             "links": [f"{l.context.trace_id:032x}" for l in (s.links or ())],
                             "attributes": dict(s.attributes or {})}) + "\n")
    return SpanExportResult.SUCCESS


def _dump_metrics(reader, path):
  """Writes {metric name: [{attributes, value or count}]} for the voice.* metrics."""
  out = {}
  data = reader.get_metrics_data()
  for rm in (data.resource_metrics if data else []):
    for sm in rm.scope_metrics:
      for m in sm.metrics:
        if m.name.startswith("voice."):
          out[m.name] = [{"attributes": dict(p.attributes),
                          "value": getattr(p, "value", None), "count": getattr(p, "count", None)}
                         for p in m.data.data_points]
  with open(path, "w", encoding="utf-8") as fh:
    json.dump(out, fh)


def _install_with_file_exporter():
  real_install()
  from google.adk.telemetry import setup as adk_setup

  readers = []
  if os.getenv("OBS_TEST_METRICS_FILE"):
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    readers.append(reader)
    atexit.register(_dump_metrics, reader, os.environ["OBS_TEST_METRICS_FILE"])
  adk_setup.maybe_set_otel_providers(
      [adk_setup.OTelHooks(span_processors=[SimpleSpanProcessor(_FileExporter(os.environ["OBS_TEST_SPANS_FILE"]))],
                           metric_readers=readers)])


def _hang_live_sessions():
  """OBS_TEST_HANG_SESSION=1: replace the Live API loop with an endless wait,
  i.e. a call that is still in progress when SIGTERM arrives."""
  import asyncio

  from streaming_core import live_session

  async def run(self, emit):
    await emit("active_agent", name="maxis_agent")
    await asyncio.sleep(3600)

  live_session.LiveVoiceSession.run = run


def _fake_live_sessions():
  """OBS_TEST_FAKE_LIVE=1: a stand-in for Gemini Live, used to test scripts/load_test.py.

  Greets with one audio chunk + turn_complete, then answers every typed
  question with one audio chunk after 0.3 s. The server, WebSocket protocol,
  audio streaming and telemetry are all real; only the model is faked.
  """
  import asyncio

  from streaming_core import live_session

  original_send_text = live_session.LiveVoiceSession.send_text

  def send_text(self, text):
    original_send_text(self, text)
    if getattr(self, "_fake_question", None) is not None:
      self._fake_question.set()

  async def run(self, emit):
    self._fake_question = asyncio.Event()
    await asyncio.sleep(0.2)
    await emit("audio", data=bytes(4800))           # greeting
    await emit("turn_complete", state="IDLE")
    while True:
      await self._fake_question.wait()
      self._fake_question.clear()
      await asyncio.sleep(0.3)                       # "model thinking"
      await emit("audio", data=bytes(4800))         # answer
      await emit("turn_complete", state="IDLE")

  live_session.LiveVoiceSession.send_text = send_text
  live_session.LiveVoiceSession.run = run


def _install():
  _install_with_file_exporter()
  if os.getenv("OBS_TEST_HANG_SESSION") == "1":
    _hang_live_sessions()
  if os.getenv("OBS_TEST_FAKE_LIVE") == "1":
    _fake_live_sessions()


launcher.install = _install

if __name__ == "__main__":
  launcher.main()
