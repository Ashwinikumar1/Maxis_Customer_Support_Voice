"""End-to-end test: the unchanged server.py run through the launcher.

Starts `python -m observability` with an in-memory-free file span exporter
(see _harness.py), opens a real voice session over the WebSocket (it fails
fast without Google credentials, which is the error path we want), probes a
cross-site WebSocket, then sends SIGTERM and checks the process exits within
Cloud Run's 10 s budget with the expected logs and spans.

Run from voice_agent/:  uv run python -m unittest discover -s observability/tests -t .
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2]


def _free_port() -> int:
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


class LauncherEndToEnd(unittest.TestCase):

  def _start(self, port, spans_file, **extra):
    env = {
        **os.environ, "PORT": str(port), "HOST": "127.0.0.1", "LOG_FORMAT": "json",
        "OTEL_TO_CLOUD": "false", "OBS_TEST_SPANS_FILE": str(spans_file),
        "GCS_BUCKET": "", "GOOGLE_CLOUD_PROJECT": "obs-test-project",
        "GOOGLE_GENAI_USE_VERTEXAI": "False", "GOOGLE_API_KEY": "invalid-test-key",
        "ALLOWED_ORIGINS": f"http://127.0.0.1:{port}", **extra,
    }
    return subprocess.Popen([sys.executable, "-m", "observability.tests._harness"], cwd=APP_DIR, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

  def test_sigterm_during_live_call_saves_transcript_and_span(self):
    port = _free_port()
    spans_file = Path(tempfile.mkdtemp()) / "spans.jsonl"
    proc = self._start(port, spans_file, OBS_TEST_HANG_SESSION="1")
    try:
      self._wait_up(port)

      async def call_then_sigterm():
        from websockets.asyncio.client import connect

        async with connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws:
          await ws.send(json.dumps({"type": "init", "timezone": "Asia/Kuala_Lumpur"}))
          async with asyncio.timeout(10):
            async for raw in ws:
              if json.loads(raw).get("type") == "active_agent":
                break
          proc.send_signal(signal.SIGTERM)
          try:
            async with asyncio.timeout(10):
              async for _ in ws:
                pass
          except Exception:
            pass

      started = time.monotonic()
      asyncio.run(call_then_sigterm())
      out, _ = proc.communicate(timeout=15)
      elapsed = time.monotonic() - started
    finally:
      if proc.poll() is None:
        proc.kill()
    self.assertLess(elapsed, 10)
    messages = [json.loads(l)["message"] for l in out.splitlines() if l.startswith("{")]
    self.assertTrue(any(m.startswith("Transcript saved") for m in messages), "in-flight transcript saved")
    self.assertTrue(any(m.startswith("Session ") and m.endswith(" closed") for m in messages))
    names = [json.loads(l)["name"] for l in spans_file.read_text().splitlines()]
    self.assertIn("voice_session", names, "in-flight call's root span ended and exported")

  def test_session_error_403_and_sigterm(self):
    port = _free_port()
    spans_file = Path(tempfile.mkdtemp()) / "spans.jsonl"
    proc = self._start(port, spans_file)
    try:
      self._wait_up(port)
      asyncio.run(self._session(port))
      self._bad_origin(port)
      started = time.monotonic()
      proc.send_signal(signal.SIGTERM)
      out, _ = proc.communicate(timeout=15)
      elapsed = time.monotonic() - started
    finally:
      if proc.poll() is None:
        proc.kill()

    self.assertLess(elapsed, 10, "must exit inside Cloud Run's SIGTERM grace period")
    entries = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
    messages = [e["message"] for e in entries]
    self.assertFalse([l for l in out.splitlines() if l.strip() and not l.startswith("{")],
                     "every log line must be JSON")
    self.assertTrue(any("WebSocket rejected: origin https://evil.example" in m for m in messages))
    self.assertTrue(any(m.startswith("SIGTERM received") for m in messages))

    session_logs = [e for e in entries if e.get("session_id")]
    self.assertTrue(session_logs, "logs inside a call must carry session_id")
    self.assertTrue(all(e["logging.googleapis.com/trace"].startswith("projects/obs-test-project/traces/")
                        for e in session_logs))

    spans = [json.loads(l) for l in spans_file.read_text().splitlines()]
    sessions = [s for s in spans if s["name"] == "voice_session"]
    self.assertTrue(sessions, "voice_session span exported")
    self.assertTrue(sessions[0]["attributes"].get("session.id", "").startswith("session_"))
    failed = [e for e in entries if "failed" in e["message"] and e["severity"] == "ERROR"]
    if failed:  # the Live API call failed (no credentials): the trace must say so too
      self.assertEqual(sessions[0]["status"], "ERROR")

  @staticmethod
  def _wait_up(port):
    for _ in range(80):
      try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
        return
      except Exception:
        time.sleep(0.25)
    raise AssertionError("server did not start")

  @staticmethod
  async def _session(port):
    from websockets.asyncio.client import connect

    try:
      async with connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({"type": "init", "timezone": "Asia/Kuala_Lumpur"}))
        async with asyncio.timeout(8):
          async for raw in ws:
            if json.loads(raw).get("type") == "error":
              break
    except Exception:
      pass  # the server closes the call after the Live API error

  @staticmethod
  def _bad_origin(port):
    request = urllib.request.Request(f"http://127.0.0.1:{port}/ws", headers={
        "Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Origin": "https://evil.example"})
    try:
      urllib.request.urlopen(request, timeout=2)
    except Exception:
      pass  # 403 expected; asserted through the server's security log


if __name__ == "__main__":
  unittest.main()
