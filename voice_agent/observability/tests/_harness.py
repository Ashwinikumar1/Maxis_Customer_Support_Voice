"""Test harness: the real launcher, with spans written to a local JSONL file.

Simulates what ADK does on Cloud Run (maybe_set_otel_providers with an
exporter) but exports to OBS_TEST_SPANS_FILE instead of Cloud Trace.
"""

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
                             "attributes": dict(s.attributes or {})}) + "\n")
    return SpanExportResult.SUCCESS


def _install_with_file_exporter():
  real_install()
  from google.adk.telemetry import setup as adk_setup

  adk_setup.maybe_set_otel_providers(
      [adk_setup.OTelHooks(span_processors=[SimpleSpanProcessor(_FileExporter(os.environ["OBS_TEST_SPANS_FILE"]))])])


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
