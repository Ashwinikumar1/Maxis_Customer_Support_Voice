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


def _install():
  _install_with_file_exporter()
  if os.getenv("OBS_TEST_HANG_SESSION") == "1":
    _hang_live_sessions()


launcher.install = _install

if __name__ == "__main__":
  launcher.main()
