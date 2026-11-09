"""Plays the ces-genesys-adapter's side of a call against our /ces bridge.

Sends exactly what the adapter's ces_ws.py sends (config, variables, session_start,
MULAW 8 kHz audio in real time, DTMF, hang-up), so a real ADK + Gemini Live call
can be tested locally without Genesys. Caller audio = a WAV (any rate, 16-bit),
played after the bot's greeting; the bot's audio is saved to --out.

  say -o /tmp/q.wav --data-format=LEI16@16000 "What roaming passes do you have for Japan?"
  uv run python ces_adapter_sim.py --wav /tmp/q.wav --msisdn 60123186001
  uv run python ces_adapter_sim.py --url wss://<cloud-run-host>/ces/test --wav /tmp/q.wav

Token: CES_BRIDGE_TOKEN from the environment or voice_agent_client/.env (same as the server).
Default --url: SERVER_URL from voice_agent_client/.env.
"""

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import uuid
import wave
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402

load_dotenv(ROOT / ".env")

import ces  # noqa: E402
from audio import pcm16_to_ulaw, resample_pcm16, ulaw_to_pcm16  # noqa: E402

FRAME, SILENCE = ces.FRAME, ces.SILENCE
GREETING_GAP_S = 1.2             # bot silent this long after speaking = its turn is over


def load_wav_as_ulaw(path: str) -> bytes:
  with wave.open(path, "rb") as w:
    if w.getsampwidth() != 2:
      raise SystemExit("WAV must be 16-bit PCM")
    pcm, rate, channels = w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()
  if channels == 2:  # keep the left channel
    pcm = b"".join(pcm[i:i + 2] for i in range(0, len(pcm), 4))
  return pcm16_to_ulaw(resample_pcm16(pcm, rate, 8000))


async def main(args):
  token = ces.load_token()
  if not token:
    raise SystemExit("Set CES_BRIDGE_TOKEN (env or .env)")
  caller_turns = [load_wav_as_ulaw(p) for p in args.wav]
  bot_audio = bytearray()
  state = {"last_bot_audio": 0.0, "bot_spoke": False, "end": None}
  conv = str(uuid.uuid4())

  local = urlparse(args.url).hostname in ("127.0.0.1", "localhost")
  async with connect(args.url, max_size=4 << 20, **({"proxy": None} if local else {}),
                     additional_headers={"Authorization": f"Bearer {token}"}) as ws:
    send = lambda m: ws.send(json.dumps(m))  # noqa: E731
    variables = {"msisdn": f"tel:+{args.msisdn}", **dict(v.split("=", 1) for v in args.var)}
    for m in ces.opening_messages(conv, variables):  # config, variables, session_start
      await send(m)
    t0 = time.monotonic()
    print(f"call {conv} started, variables {variables}")

    async def receive():
      async for raw in ws:
        msg = json.loads(raw)
        now = time.monotonic() - t0
        if "sessionOutput" in msg and "audio" in msg["sessionOutput"]:
          chunk = base64.b64decode(msg["sessionOutput"]["audio"])
          if not state["bot_spoke"] or now - state["last_bot_audio"] > GREETING_GAP_S:
            print(f"[{now:6.2f}s] bot speaking")
          bot_audio.extend(chunk)
          state["bot_spoke"], state["last_bot_audio"] = True, now
        elif "interruptionSignal" in msg:
          print(f"[{now:6.2f}s] interruptionSignal (barge-in)")
        elif "endSession" in msg:
          state["end"] = msg["endSession"].get("metadata", {}).get("params", {})
          print(f"[{now:6.2f}s] endSession -> outputVariables {state['end']}")
        else:
          print(f"[{now:6.2f}s] {list(msg)}")

    async def caller():
      """Like a real phone line: a continuous 20 ms audio stream, speech when it's our turn."""
      queue = list(caller_turns)
      next_t = time.monotonic()
      speech = b""
      while True:
        now = time.monotonic() - t0
        bot_done = state["bot_spoke"] and now - state["last_bot_audio"] > GREETING_GAP_S
        if not speech and queue and bot_done:
          speech = queue.pop(0)
          state["bot_spoke"] = False  # wait for the next reply before the next turn
          print(f"[{now:6.2f}s] caller speaking ({len(speech) / 8000:.1f}s)")
        if not speech and not queue and bot_done:
          await asyncio.sleep(args.tail)
          return "done"
        frame, speech = (speech[:FRAME], speech[FRAME:]) if speech else (SILENCE, b"")
        await send({"realtimeInput": {"audio": base64.b64encode(frame.ljust(FRAME, b"\xff")).decode()}})
        next_t += 0.02
        await asyncio.sleep(max(0, next_t - time.monotonic()))

    tasks = [asyncio.create_task(receive()), asyncio.create_task(caller())]
    try:
      done, _ = await asyncio.wait(tasks, timeout=args.timeout, return_when=asyncio.FIRST_COMPLETED)
      if tasks[1] in done and state["end"] is None:  # we finished talking: hang up like Genesys
        print("caller hangs up")
        for m in ces.hangup_messages():
          await send(m)
        await asyncio.wait_for(tasks[0], 10)
    except (ConnectionClosed, TimeoutError):
      pass
    finally:
      for t in tasks:
        t.cancel()

  with wave.open(args.out, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    w.writeframes(ulaw_to_pcm16(bytes(bot_audio)))
  print(f"bot audio: {len(bot_audio) / 8000:.1f}s -> {args.out}; outcome: {state['end']}")


if __name__ == "__main__":
  p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  p.add_argument("--url", default=os.getenv("SERVER_URL", "ws://127.0.0.1:8080/ces/local"))
  p.add_argument("--wav", action="append", required=True, help="caller turn (repeat for more turns)")
  p.add_argument("--msisdn", default="60123186001")
  p.add_argument("--var", action="append", default=[], help="extra Architect variable k=v")
  p.add_argument("--out", default="bot_audio.wav")
  p.add_argument("--tail", type=float, default=3.0, help="seconds of silence after the last reply")
  p.add_argument("--timeout", type=float, default=120.0)
  asyncio.run(main(p.parse_args()))
