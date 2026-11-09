"""Our server (real server.py + ces_bridge) with a stand-in for Gemini Live.

The stand-in: greets with 1.5 s of tone on the kickstart; when the caller's audio gets loud
(speech), barges in, answers with 0.6 s of tone, then ends the call (endCall "resolved").
env: SERVER_DIR, PORT, CES_BRIDGE_TOKEN
"""
import asyncio
import math
import os
import struct
import sys

os.environ.update(OTEL_TO_CLOUD="false", CES_DEBUG_EVENTS="false")
sys.path.insert(0, os.environ["SERVER_DIR"])

import server  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402


def tone(seconds, rate=24000, hz=440):
  return b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / rate)))
                  for i in range(int(seconds * rate)))


class StubCall:
  def __init__(self, info):
    self.info, self.outcome, self.ended = info, None, asyncio.Event()
    self._kick, self._speech = asyncio.Event(), asyncio.Event()
    print(f"[server] call {info.conversation_id} msisdn={info.msisdn} vars={sorted(info.channel_vars)}", flush=True)

  def send_text(self, text):
    print(f"[server] user text: {text!r}", flush=True)
    self._kick.set()

  def send_audio(self, pcm16, rate=16000):
    peak = max((abs(s) for (s,) in struct.iter_unpack("<h", pcm16[: len(pcm16) // 2 * 2])), default=0)
    if peak > 2000 and not self._speech.is_set():
      print("[server] caller speech detected", flush=True)
      self._speech.set()

  async def run(self, emit):
    await self._kick.wait()
    for i in range(0, 15):                       # greeting: 1.5 s in 100 ms chunks, real time
      await emit("audio", data=tone(0.1))
      await asyncio.sleep(0.1)
    await self._speech.wait()
    await emit("interrupted")
    await emit("audio", data=tone(0.6, hz=660))
    await asyncio.sleep(0.6)
    self.outcome = {"action": "end", "reason": "resolved"}
    self.ended.set()
    await asyncio.sleep(3600)

  async def finish(self):
    print(f"[server] call {self.info.conversation_id} finished", flush=True)


async def start_call(info):
  return StubCall(info)


async def main():
  server._start_call = start_call
  async with serve(server.handle_client, "127.0.0.1", int(os.environ["PORT"]),
                   process_request=server.process_request) as s:
    print(f"[server] listening on {os.environ['PORT']}", flush=True)
    await s.serve_forever()

asyncio.run(main())
