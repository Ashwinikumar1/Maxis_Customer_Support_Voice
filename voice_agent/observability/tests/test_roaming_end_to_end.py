"""End-to-end: logging, tracing, metrics and transcripts for real roaming calls.

Runs the real server through the observability launcher with a scripted model
(observability/tests/_roaming_harness.py): the agents, guardrails, roaming tools, mock
gateway, telemetry and transcripts are all real; only Gemini is replaced. Three calls
over the real WebSocket:

  buy      buy a pass (contained)
  down     the gateway fails twice, the caller is escalated
  refused  a caller at verification level 1 is refused the purchase by the tool policy

and checks every log line, span, metric and transcript field that roaming calls produce.

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

import websockets

APP_DIR = Path(__file__).resolve().parents[2]
CALLER_MSISDN = "60100000001"   # the "principal" demo caller: must never appear in telemetry


def _free_port() -> int:
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


def _run_calls(calls: str, verification_level: str) -> dict:
  """Starts the harness server, makes the calls, stops it; returns logs, spans, metrics, transcripts."""
  port, out = _free_port(), Path(tempfile.mkdtemp())
  env = {
      **os.environ, "PORT": str(port), "HOST": "127.0.0.1", "LOG_FORMAT": "json", "OTEL_TO_CLOUD": "false",
      "OBS_TEST_SPANS_FILE": str(out / "spans.jsonl"), "OBS_TEST_METRICS_FILE": str(out / "metrics.json"),
      "GCS_BUCKET": "", "GOOGLE_CLOUD_PROJECT": "obs-roaming-test", "ALLOWED_ORIGINS": f"http://127.0.0.1:{port}",
      "ROAMING_CALLS": calls, "TEST_CALLER_VERIFICATION_LEVEL": verification_level, "GATEWAY_URL": "mock",
      "ALLOW_TEST_CALLERS": "true", "WRITE_TOOLS_ENABLED": "", "MAX_WRITES_PER_CALL": "",
      "GREET_ON_CONNECT": "false", "GOOGLE_GENAI_USE_VERTEXAI": "False", "GOOGLE_API_KEY": "unused",
  }
  proc = subprocess.Popen([sys.executable, "-m", "observability.tests._roaming_harness"], cwd=APP_DIR, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
  ended = []
  try:
    for _ in range(120):
      try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
        break
      except Exception:
        time.sleep(0.25)

    async def call():
      async with websockets.connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "init", "timezone": "Asia/Kuala_Lumpur", "caller": "principal"}))
        while True:
          message = json.loads(await asyncio.wait_for(ws.recv(), 40))
          if message["type"] == "call_ended":
            ended.append(True)
            return

    for _ in calls.split(","):
      asyncio.run(call())
    time.sleep(1)
    proc.send_signal(signal.SIGTERM)
    stdout, _ = proc.communicate(timeout=20)
  finally:
    if proc.poll() is None:
      proc.kill()
  spans = [json.loads(l) for l in (out / "spans.jsonl").read_text().splitlines()]
  sessions = {s["attributes"]["session.id"] for s in spans if s["name"] == "voice_session"}
  transcripts = []
  for path in (APP_DIR / "transcripts").glob("conversation_*.json"):
    if any(f"conversation_{sid}_" in path.name for sid in sessions):
      transcripts.append(json.loads(path.read_text()))
      path.unlink()
  return {
      "ended": ended, "stdout": stdout, "spans": spans,
      "logs": [json.loads(l) for l in stdout.splitlines() if l.startswith("{")],
      "plain": [l for l in stdout.splitlines() if l.strip() and not l.startswith("{")],
      "metrics": json.loads((out / "metrics.json").read_text()), "transcripts": transcripts,
  }


class RoamingCallsEndToEnd(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    cls.run1 = _run_calls("buy,down", verification_level="2")
    cls.run2 = _run_calls("refused", verification_level="1")
    calls = [s for s in cls.run1["spans"] if s["name"] == "voice_session"]
    cls.buy = next(c for c in calls if c["attributes"].get("voice.call_outcome") == "contained")
    cls.down = next(c for c in calls if c["attributes"].get("voice.call_outcome") == "escalated")

  def children(self, call, run=None):
    return [s for s in (run or self.run1)["spans"] if s["trace_id"] == call["trace_id"] and s["name"] != "voice_session"]

  def logs_of(self, call, run=None):
    return [l for l in (run or self.run1)["logs"] if l.get("session_id") == call["attributes"]["session.id"]]

  # ------------------------------------------------------------------ the calls themselves
  def test_every_call_ended_through_end_session(self):
    self.assertEqual(len(self.run1["ended"]), 2)
    self.assertEqual(len(self.run2["ended"]), 1)

  # ------------------------------------------------------------------ traces
  def test_call_span_records_outcome_agent_and_handover(self):
    for call, outcome, escalated in ((self.buy, "contained", False), (self.down, "escalated", True)):
      a = call["attributes"]
      self.assertEqual(a["voice.close_reason"], "agent_end_session")
      self.assertEqual(a["voice.call_outcome"], outcome)
      self.assertEqual(a["voice.last_agent"], "roaming_agent")
      self.assertEqual(a["voice.handover.escalated"], escalated)
      self.assertEqual((a["voice.handover.intent"], a["voice.handover.use_case"]), ("roaming", "buy_a_pass"))
      self.assertEqual(a["voice.verification_level"], 2)
      self.assertEqual(a["session.id"], a["session.ref"])  # the hashed ID, one value everywhere
      self.assertFalse(a["session.id"].startswith("session_"))

  def test_tool_and_gateway_spans_on_the_happy_path(self):
    kids = self.children(self.buy)
    names = [s["name"] for s in kids]
    for name in ("execute_tool transfer_to_agent", "execute_tool listRoamingLines", "execute_tool findPasses",
                 "execute_tool buyPass", "execute_tool end_session", "invoke_agent roaming_agent"):
      self.assertIn(name, names)
    gateway = [(s["attributes"]["agent_gateway.operation"], s["attributes"].get("agent_gateway.next_step"))
               for s in kids if s["name"] == "agent_gateway.request"]
    self.assertEqual(gateway, [("listRoamingLines", "confirm_which_line_if_unclear"), ("findPasses", "recommend_pass"),
                               ("buyPass", "confirm_purchase"), ("buyPass", "purchase_done")])
    self.assertFalse([s["name"] for s in kids if s["status"] == "ERROR"])
    self.assertTrue(all(s["attributes"].get("session.id") == self.buy["attributes"]["session.id"] for s in kids))

  def test_gateway_failure_marks_gateway_and_tool_spans_error(self):
    kids = self.children(self.down)
    gateway = [s for s in kids if s["name"] == "agent_gateway.request"]
    self.assertEqual(len(gateway), 2)
    for s in gateway:
      self.assertEqual(s["status"], "ERROR")
      self.assertEqual(s["attributes"]["agent_gateway.violation_code"], "BACKEND_UNAVAILABLE")
    tools = [s for s in kids if s["name"] == "execute_tool findPasses"]
    self.assertTrue(tools and all(s["status"] == "ERROR" for s in tools))

  def test_model_turns_time_to_first_audio_and_tokens(self):
    """The live-audio telemetry: one model_turn per reply, named by agent, with time to first audio."""
    turns = [s for s in self.children(self.buy) if s["name"] == "model_turn"]
    self.assertGreaterEqual(len(turns), 4)
    agents = {t["attributes"].get("gen_ai.agent.name") for t in turns}
    self.assertIn("roaming_agent", agents)
    answered = [t for t in turns if t["attributes"].get("voice.turn_trigger") == "user_text"]
    self.assertTrue(answered and all("voice.time_to_first_audio_ms" in t["attributes"] for t in answered))
    a = self.buy["attributes"]
    self.assertEqual(a["voice.tokens.total"], 240 * 4)  # the last running total of the call's 4 replies
    ttfa = self.run1["metrics"].get("voice.time_to_first_audio", [])
    self.assertIn("roaming_agent", {p["attributes"].get("agent") for p in ttfa})

  def test_nothing_personal_in_spans(self):
    blob = json.dumps(self.run1["spans"] + self.run2["spans"])
    self.assertNotIn(CALLER_MSISDN, blob)
    gateway = [s for s in self.run1["spans"] if s["name"] == "agent_gateway.request"]
    self.assertNotIn("Tokyo", json.dumps(gateway))  # tool arguments are never recorded

  # ------------------------------------------------------------------ logs
  def test_logs_are_json_and_follow_the_call(self):
    self.assertFalse(self.run1["plain"], "every log line must be JSON")
    for call, outcome in ((self.buy, "outcome=contained"), (self.down, "outcome=escalated")):
      sid = call["attributes"]["session.id"]
      mine = self.logs_of(call)
      messages = [l.get("message", "") for l in mine]
      self.assertTrue(any(m.startswith(f"Session {sid} started") for m in messages))
      self.assertTrue(any("active agent roaming_agent" in m for m in messages))
      self.assertTrue(any("end_session by roaming_agent" in m for m in messages))
      closed = [m for m in messages if " closed (reason=agent_end_session" in m]
      self.assertTrue(closed and outcome in closed[0], closed)
      traced = [l for l in mine if "logging.googleapis.com/trace" in l]
      self.assertTrue(traced and all(l["logging.googleapis.com/trace"].endswith(call["trace_id"]) for l in traced))
      self.assertTrue(any(l.get("security_event") == "verification_level_set" for l in mine))
      self.assertFalse([m for m in messages if "conversation_session_" in m], "raw session ID in a log line")

  def test_each_gateway_failure_logged_once(self):
    errors = [l for l in self.logs_of(self.down) if l.get("severity") == "ERROR"]
    self.assertEqual(len(errors), 2, [e["message"] for e in errors])
    self.assertTrue(all("Gateway GET /roaming/passes" in e["message"] for e in errors))

  def test_no_noise_and_nothing_personal_in_logs(self):
    self.assertFalse([l for l in self.run1["logs"] if l.get("logger", "").startswith(("httpx", "httpcore"))])
    self.assertNotIn(CALLER_MSISDN, self.run1["stdout"] + self.run2["stdout"])
    self.assertFalse([l for l in self.run1["logs"] if "Skipping missing token usage" in l.get("message", "")])

  # ------------------------------------------------------------------ metrics
  def test_call_and_gateway_metrics(self):
    m = self.run1["metrics"]
    calls = {(p["attributes"]["outcome"], p["attributes"]["last_agent"]): p["value"] for p in m["voice.calls"]}
    self.assertEqual(calls.get(("contained", "roaming_agent")), 1)
    self.assertEqual(calls.get(("escalated", "roaming_agent")), 1)
    gateway = {(p["attributes"]["operation"], p["attributes"]["outcome"]): p["count"] for p in m["voice.gateway.duration"]}
    self.assertEqual(gateway.get(("buyPass", "ok")), 2)
    self.assertEqual(gateway.get(("findPasses", "fail_BACKEND_UNAVAILABLE")), 2)
    self.assertIn("agent_end_session", {p["attributes"]["close_reason"] for p in m["voice.sessions"]})
    self.assertTrue(m.get("voice.session.duration"))
    self.assertTrue(m.get("voice.call.tokens"))

  # ------------------------------------------------------------------ transcripts
  def test_transcripts(self):
    self.assertEqual(len(self.run1["transcripts"]), 2)
    for t in self.run1["transcripts"]:
      meta = t["metadata"]
      for key in ("outcome", "trace_id", "session_ref", "redaction", "token_usage"):
        self.assertTrue(meta.get(key), key)
      self.assertTrue(any(e.get("event") == "end_session" and e["params"]["intent"] == "roaming"
                          for e in t["conversation"]))
      self.assertTrue(meta["caller_id"].endswith("0001"))
      self.assertNotIn(CALLER_MSISDN, json.dumps(t))

  # ------------------------------------------------------------------ security
  def test_refused_purchase_is_visible_and_never_reaches_the_gateway(self):
    run = self.run2
    call = next(s for s in run["spans"] if s["name"] == "voice_session")
    refused = [s for s in run["spans"] if s["name"] == "execute_tool buyPass"
               and s["attributes"].get("guardrails.refused_reason")]
    self.assertEqual(refused[0]["attributes"]["guardrails.refused_reason"], "verification_required")
    events = [l for l in run["logs"] if l.get("security_event") == "policy_denied"]
    self.assertEqual(events[0]["session_id"], call["attributes"]["session.id"])
    self.assertTrue(events[0]["logging.googleapis.com/trace"].endswith(call["trace_id"]))
    self.assertEqual(call["attributes"]["voice.verification_level"], 1)
    gateway = [s["attributes"].get("agent_gateway.next_step") for s in run["spans"] if s["name"] == "agent_gateway.request"]
    self.assertEqual(gateway, ["confirm_purchase"])  # only the preview; the refused purchase never went out


if __name__ == "__main__":
  unittest.main()
