"""Offline test of the CES bridge (/ces) - no Gemini, no network.

A fake adapter sends exactly what ces-genesys-adapter's ces_ws.py sends; the ADK
Call is replaced by a stub. Checks auth, setup, audio both ways, DTMF, barge-in
and the endSession -> Genesys outputVariables mapping.

Run:  cd voice_agent && uv run python scripts/test_ces_bridge.py
"""

import asyncio
import base64
import json
import os
import secrets
import sys
from pathlib import Path

TOKEN = secrets.token_urlsafe(32)
os.environ.update(CES_BRIDGE_ENABLED="true", OTEL_TO_CLOUD="false",
                  CES_BRIDGE_TOKEN=json.dumps({"access_token": TOKEN, "expiry": 9999999999999}))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


async def main():
  server._start_call = fake_start_call
  async with serve(server.handle_client, "127.0.0.1", 0, process_request=server.process_request) as srv:
    port = srv.sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}/ces/us"

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
      print("ok  bot audio -> sessionOutput.audio (MULAW 8 kHz)")

      await ws.send(json.dumps({"realtimeInput": {"dtmf": "5"}}))
      for _ in range(3):
        await ws.send(json.dumps({"realtimeInput": {"audio": base64.b64encode(ULAW_SILENCE).decode()}}))
      assert "interruptionSignal" in json.loads(await ws.recv())
      print("ok  barge-in -> interruptionSignal")

      end = json.loads(await ws.recv())
      params = end["endSession"]["metadata"]["params"]
      assert params == {"outcome": "handoff", "handoff_reason": "caller asked",
                        "summary": "Japan pass", "use_case": "buy_a_pass"}, params
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
      assert end == {"endSession": {"metadata": {"params": {}}}}, end
    assert FakeCall.last.info.msisdn == "anonymous"
    print("ok  caller hang-up -> endSession, no number -> 'anonymous'")
  print("\nALL CES BRIDGE CHECKS PASSED")


if __name__ == "__main__":
  asyncio.run(main())
