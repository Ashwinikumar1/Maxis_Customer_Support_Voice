"""Offline test of the server's /ces endpoint - no Gemini, no network.

A fake adapter sends exactly what ces-genesys-adapter's ces_ws.py sends; the ADK
Call is replaced by a stub. Checks routing (/healthz, nothing else), auth, setup,
audio both ways, DTMF, barge-in, the endSession -> Genesys outputVariables mapping,
and the test-client extras (typed text, timezone, debug events only on request).

Run:  ../voice_agent_server/.venv/bin/python scripts/test_ces_bridge.py
"""

import asyncio
import base64
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

TOKEN = secrets.token_urlsafe(32)
os.environ.update(CES_DEBUG_EVENTS="true", OTEL_TO_CLOUD="false",
                  CES_BRIDGE_TOKEN=json.dumps({"access_token": TOKEN, "expiry": 9999999999999}))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "voice_agent_server"))

import server  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402
from websockets.exceptions import ConnectionClosed, InvalidStatus  # noqa: E402

ULAW_SILENCE = b"\xff" * 160  # 20 ms of PCMU 8 kHz


class FakeCall:
  """Stands in for streaming_core.Call: greets on the kickstart, gets interrupted by
  the caller's audio, then hands off (like escalateToAgent)."""
  last = None

  def __init__(self, info):
    self.info, self.outcome, self.ended = info, None, asyncio.Event()
    self.texts, self.audio_bytes, self.finished = [], 0, False
    self._events: asyncio.Queue = asyncio.Queue()
    FakeCall.last = self

  def send_text(self, text):
    self.texts.append(text)
    self._events.put_nowait("text")

  def send_audio(self, pcm16, rate=16000):
    assert rate == 16000
    self.audio_bytes += len(pcm16)
    if self.audio_bytes >= 3 * 640:  # 3 frames of 20 ms at 16 kHz PCM16
      self._events.put_nowait("audio")

  async def run(self, emit):
    assert await self._events.get() == "text"            # kickstart -> greeting
    await emit("transcript", role="agent", text="Hello!", partial=False, author="maxis_agent")
    await emit("audio", data=b"\x10\x00" * 2400)          # 100 ms of PCM16 24 kHz
    while await self._events.get() != "audio":           # caller speaks -> barge-in
      pass                                               # (skip DTMF text turns)
    await emit("interrupted")
    self.outcome = {"action": "handoff", "reason": "caller asked", "customer_request": "Japan pass",
                    "use_case": "buy_a_pass"}
    self.ended.set()
    await asyncio.sleep(3600)

  async def finish(self):
    self.finished = True


async def fake_start_call(info):
  return FakeCall(info)


def _http_status(url: str) -> int:
  opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback: no proxy
  try:
    with opener.open(url, timeout=5) as r:
      return r.status
  except urllib.error.HTTPError as e:
    return e.code


async def main():
  server._start_call = fake_start_call
  async with serve(server.handle_client, "127.0.0.1", 0, process_request=server.process_request) as srv:
    port = srv.sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}/ces/us"

    # 0. Routing: health check only; no UI, no browser /ws on the server any more.
    for path, want in (("/healthz", 200), ("/", 404), ("/ws", 404), ("/static/app.js", 404)):
      got = await asyncio.to_thread(_http_status, f"http://127.0.0.1:{port}{path}")
      assert got == want, (path, got)
    print("ok  /healthz 200; /, /ws, /static -> 404")

    # 1. Auth: no token / wrong token -> 401 before any WebSocket opens.
    for headers in ({}, {"Authorization": "Bearer wrong-token-wrong-token-wrong-token"}):
      try:
        async with connect(url, proxy=None, additional_headers=headers):
          raise AssertionError("unauthenticated connection was accepted")
      except InvalidStatus as e:
        assert e.response.status_code == 401, e.response.status_code
    print("ok  401 without a valid token")

    # 2. A full call, message for message like ces_ws.py.
    async with connect(url, proxy=None, additional_headers={"Authorization": f"Bearer {TOKEN}",
                                                "X-Goog-User-Project": "test"}) as ws:
      conv = "6f1c2b8e-0d1a-4c55-9b7e-1e2f3a4b5c6d"
      await ws.send(json.dumps({"config": {
          "session": f"projects/p/locations/us/apps/adk/sessions/{conv}",
          "inputAudioConfig": {"audioEncoding": "MULAW", "sampleRateHertz": 8000},
          "outputAudioConfig": {"audioEncoding": "MULAW", "sampleRateHertz": 8000}}}))
      await ws.send(json.dumps({"realtimeInput": {"variables": {
          "msisdn": "tel:+60123186001", "language": "ms", "adapterSessionId": "abc",
          "bad key!": "dropped"}}}))
      await ws.send(json.dumps({"realtimeInput": {"event": {"event": "session_start"}}}))

      msg = json.loads(await ws.recv())
      ulaw = base64.b64decode(msg["sessionOutput"]["audio"])
      assert len(ulaw) == 800, len(ulaw)  # 100 ms at 8 kHz, 1 byte/sample
      print("ok  bot audio -> sessionOutput.audio (MULAW 8 kHz); no debug events for Genesys")

      await ws.send(json.dumps({"realtimeInput": {"dtmf": "5"}}))
      for _ in range(3):
        await ws.send(json.dumps({"realtimeInput": {"audio": base64.b64encode(ULAW_SILENCE).decode()}}))
      assert "interruptionSignal" in json.loads(await ws.recv())
      print("ok  barge-in -> interruptionSignal")

      end = json.loads(await ws.recv())
      meta = end["endSession"]["metadata"]
      params = meta["params"]
      assert meta["session_escalated"] is True and meta["reason"] == "caller asked", meta
      assert params == {"outcome": "handoff", "session_escalated": "true",
                        "handoff_reason": "caller asked", "summary": "Japan pass",
                        "customer_request": "Japan pass", "use_case": "buy_a_pass"}, params
      assert all(isinstance(v, str) for v in params.values())  # Genesys outputVariables: strings
      print("ok  endSession params (-> Genesys outputVariables):", params)
      try:
        await ws.recv()
        raise AssertionError("bridge did not close after endSession")
      except ConnectionClosed as e:
        assert e.rcvd.code == 1000, e.rcvd
      print("ok  closed normally (1000)")

    call = FakeCall.last
    assert call.info.msisdn == "60123186001" and call.info.conversation_id == conv
    assert call.info.channel == "genesys"
    assert call.info.channel_vars == {"msisdn": "tel:+60123186001", "language": "ms",
                                      "adapterSessionId": "abc"}, call.info.channel_vars
    assert call.texts == ["Hello", "[The caller pressed 5 on the keypad]"], call.texts
    assert call.audio_bytes == 3 * 640, call.audio_bytes  # 160 B PCMU 8k -> 640 B PCM16 16k
    await asyncio.sleep(0.1)
    assert call.finished
    print("ok  CallInfo: msisdn from variables, conversationId as session, vars cleaned, DTMF")

    # 3. Caller hangs up mid-call: adapter sends the disconnect event + half close.
    async with connect(url, proxy=None, additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
      await ws.send(json.dumps({"config": {"session": "x/sessions/hangup-test"}}))
      await ws.send(json.dumps({"realtimeInput": {"event": {"event": "session_start"}}}))
      await ws.recv()  # greeting audio
      await ws.send(json.dumps({"realtimeInput": {"event": {"event": "sys.remote-call-disconnected"}}}))
      await ws.send(json.dumps({"clientHalfClose": True}))
      end = json.loads(await ws.recv())
      assert end == {"endSession": {"metadata": {"session_escalated": False, "reason": "caller_hung_up",
                                                 "params": {}}}}, end
    assert FakeCall.last.info.msisdn == "anonymous"
    print("ok  caller hang-up -> endSession, no number -> 'anonymous'")

    # 3b. Exactly what the deployed adapter sends for the GECX flow: Architect's `_` variables
    # are stripped, `caller_id` carries the number, adapterSessionId is added, and
    # `_initial_message` becomes a text kickstart instead of session_start.
    async with connect(url, proxy=None, additional_headers={"Authorization": f"Bearer {TOKEN}",
                                                            "X-Goog-User-Project": "mxs"}) as ws:
      await ws.send(json.dumps({"config": {
          "session": "projects/p/locations/us/apps/adk/sessions/0f8fad5b-d9cb-469f-a165-70867728950e",
          "inputAudioConfig": {"audioEncoding": "MULAW", "sampleRateHertz": 8000},
          "outputAudioConfig": {"audioEncoding": "MULAW", "sampleRateHertz": 8000}}}))
      await ws.send(json.dumps({"realtimeInput": {"variables": {
          "caller_id": "+60164129768", "adapterSessionId": "7c9e6679-7425-40de-944b-e07fc1f90ae7"}}}))
      await ws.send(json.dumps({"realtimeInput": {"text": "Hi"}}))
      assert "sessionOutput" in json.loads(await ws.recv())
      for _ in range(3):
        await ws.send(json.dumps({"realtimeInput": {"audio": base64.b64encode(ULAW_SILENCE).decode()}}))
      assert "interruptionSignal" in json.loads(await ws.recv())
      assert json.loads(await ws.recv())["endSession"]["metadata"]["params"]["outcome"] == "handoff"
    call = FakeCall.last
    assert call.info.msisdn == "60164129768", call.info.msisdn
    assert call.info.conversation_id == "0f8fad5b-d9cb-469f-a165-70867728950e"
    assert call.texts == ["Hi"], call.texts
    print("ok  adapter-shaped call: caller_id -> MSISDN, _initial_message text kickstart")

    # 4. Test client (../client): asks for debug events, sends timezone + typed text.
    async with connect(url, proxy=None, additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
      await ws.send(json.dumps({"config": {"session": "x/sessions/client-test"}}))
      await ws.send(json.dumps({"realtimeInput": {"variables": {
          "msisdn": "tel:+60164129768", "timezone": "Asia/Kuala_Lumpur", "debug_events": "true"}}}))
      await ws.send(json.dumps({"realtimeInput": {"event": {"event": "session_start"}}}))
      first = json.loads(await ws.recv())
      assert first == {"debugEvent": {"type": "transcript", "role": "agent", "text": "Hello!",
                                      "partial": False, "author": "maxis_agent"}}, first
      assert "sessionOutput" in json.loads(await ws.recv())
      await ws.send(json.dumps({"realtimeInput": {"text": "What passes for Japan?"}}))
      for _ in range(3):
        await ws.send(json.dumps({"realtimeInput": {"audio": base64.b64encode(ULAW_SILENCE).decode()}}))
      assert "interruptionSignal" in json.loads(await ws.recv())
      assert "endSession" in json.loads(await ws.recv())
    call = FakeCall.last
    assert call.info.timezone == "Asia/Kuala_Lumpur", call.info.timezone
    assert call.texts == ["Hello", "What passes for Japan?"], call.texts
    print("ok  test client: debug events on request, typed text, timezone")
  print("\nALL CES BRIDGE CHECKS PASSED")


if __name__ == "__main__":
  asyncio.run(main())
