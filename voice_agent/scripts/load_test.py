"""Load test for the deployed voice agent: many simulated callers at once.

WHAT IT DOES (per simulated caller)
  1. Opens the same WebSocket the web page uses (/ws), with the service URL as
     Origin (the server rejects other origins).
  2. Sends {"type":"init"} and streams microphone-sized audio chunks for the
     whole call: 100 ms of PCM16 16 kHz each, silence by default or a speech
     recording (--wav). This is the network/CPU load a real caller creates.
  3. Waits for the greeting to finish (turn_complete), then sends a typed
     question and measures how long until the first audio of the answer
     arrives: the delay a caller would hear.
  4. Keeps the call open for --duration seconds, then ends it with {"type":"end"}.

WHAT IT REPORTS
  Calls connected / answered / failed (with reasons), and p50 / p95 / max for:
  connect time, greeting (connect -> first audio), answer (question -> first audio).

COST: every simulated call is a real Gemini Live session. Audio is billed at
32 tokens per second (https://ai.google.dev/gemini-api/docs/audio), so a
60-second call sends ~1,920 tokens of audio plus the model's replies.
Start with --calls 5 and increase step by step (see the test runbook).

USAGE (from voice_agent/):
  uv run python scripts/load_test.py --url https://SERVICE-URL --calls 10 --ramp 20 --duration 60
"""

import argparse
import asyncio
import base64
import json
import statistics
import sys
import time
import wave
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlparse

from websockets.asyncio.client import connect

CHUNK_MS = 100                    # one microphone chunk, as the web page sends
SAMPLE_RATE = 16000               # PCM16 mono 16 kHz: what the server expects
CHUNK_BYTES = SAMPLE_RATE * 2 * CHUNK_MS // 1000


@dataclass
class CallResult:
  index: int
  connect_ms: float | None = None
  greeting_ms: float | None = None
  answer_ms: float | None = None
  error: str | None = None
  events: Counter = field(default_factory=Counter)


def _load_audio(wav_path: str | None) -> list[bytes]:
  """100 ms chunks to stream: from a 16 kHz mono 16-bit WAV, or one chunk of silence."""
  if not wav_path:
    return [bytes(CHUNK_BYTES)]
  with wave.open(wav_path, "rb") as wav:
    if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (SAMPLE_RATE, 1, 2):
      sys.exit(f"{wav_path}: must be 16 kHz, mono, 16-bit PCM (got {wav.getframerate()} Hz, "
               f"{wav.getnchannels()} channel(s), {8 * wav.getsampwidth()}-bit)")
    pcm = wav.readframes(wav.getnframes())
  chunks = [pcm[i:i + CHUNK_BYTES] for i in range(0, len(pcm), CHUNK_BYTES)]
  return [c for c in chunks if len(c) == CHUNK_BYTES] or [bytes(CHUNK_BYTES)]


async def one_call(i: int, args, ws_url: str, origin: str, audio: list[bytes]) -> CallResult:
  result = CallResult(i)
  t0 = time.monotonic()
  try:
    async with connect(ws_url, origin=origin, open_timeout=20, max_size=2**22) as ws:
      result.connect_ms = (time.monotonic() - t0) * 1000
      await ws.send(json.dumps({"type": "init", "timezone": args.timezone}))
      greeting_done = asyncio.Event()
      question_sent_at = None

      async def stream_audio():
        n = 0
        while True:  # loops the recording (or silence) for the whole call
          await ws.send(json.dumps({"type": "audio", "data": base64.b64encode(audio[n % len(audio)]).decode()}))
          n += 1
          await asyncio.sleep(CHUNK_MS / 1000)

      async def receive():
        async for raw in ws:
          msg = json.loads(raw)
          kind = msg.get("type")
          result.events[kind] += 1
          now = time.monotonic()
          if kind == "audio":
            if result.greeting_ms is None:
              result.greeting_ms = (now - t0) * 1000
            if question_sent_at is not None and result.answer_ms is None:
              result.answer_ms = (now - question_sent_at) * 1000
          elif kind == "turn_complete" and msg.get("state", "IDLE") == "IDLE":
            greeting_done.set()
          elif kind == "error":
            raise RuntimeError(f"server error: {msg.get('message')}")

      streamer = asyncio.create_task(stream_audio())
      receiver = asyncio.create_task(receive())
      try:
        # Wait for the greeting to finish so its audio is not mistaken for the answer.
        try:
          await asyncio.wait_for(asyncio.shield(greeting_done.wait()), timeout=args.greeting_timeout)
        except TimeoutError:
          pass  # no greeting (or it never completed): ask anyway
        if receiver.done():
          receiver.result()  # re-raise a server error
        question_sent_at = time.monotonic()
        await ws.send(json.dumps({"type": "text", "data": args.question}))
        # Hold the call for its remaining duration, still streaming audio.
        remaining = max(0.0, args.duration - (time.monotonic() - t0))
        done, _ = await asyncio.wait({receiver}, timeout=remaining)
        if done:
          receiver.result()  # the server ended the call early: re-raise its error, if any
          result.error = result.error or "server closed the call early"
        else:
          await ws.send(json.dumps({"type": "end"}))
      finally:
        streamer.cancel()
        receiver.cancel()
      if result.answer_ms is None and result.error is None:
        result.error = "no answer audio"
  except Exception as exc:  # each failure is reported, never hidden
    result.error = result.error or f"{type(exc).__name__}: {str(exc)[:120]}"
  return result


def _stats(values: list[float]) -> str:
  if not values:
    return "n/a"
  values = sorted(values)
  p95 = values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]
  return f"p50 {statistics.median(values):7.0f} ms   p95 {p95:7.0f} ms   max {values[-1]:7.0f} ms"


async def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--url", required=True, help="service URL, e.g. https://maxis-voice-agent-123.asia-southeast1.run.app")
  parser.add_argument("--calls", type=int, default=5, help="number of simultaneous simulated callers")
  parser.add_argument("--ramp", type=float, default=10, help="seconds over which the calls are started")
  parser.add_argument("--duration", type=float, default=60, help="seconds each call stays open")
  parser.add_argument("--question", default="How do I activate roaming?", help="typed question asked after the greeting")
  parser.add_argument("--wav", help="16 kHz mono 16-bit WAV to stream instead of silence")
  parser.add_argument("--timezone", default="Asia/Kuala_Lumpur")
  parser.add_argument("--greeting-timeout", type=float, default=20, help="max seconds to wait for the greeting")
  parser.add_argument("--max-p95-answer-ms", type=float, default=3000, help="pass/fail threshold for answer p95")
  args = parser.parse_args()

  parsed = urlparse(args.url)
  if parsed.scheme not in ("http", "https") or not parsed.netloc:
    sys.exit("--url must look like https://SERVICE-URL")
  origin = f"{parsed.scheme}://{parsed.netloc}"
  ws_url = f"{'wss' if parsed.scheme == 'https' else 'ws'}://{parsed.netloc}/ws"
  audio = _load_audio(args.wav)

  print(f"Load test: {args.calls} calls to {ws_url}, started over {args.ramp:.0f} s, "
        f"{args.duration:.0f} s each, streaming {'speech from ' + args.wav if args.wav else 'silence'}")
  started = time.monotonic()

  async def delayed(i):
    await asyncio.sleep(args.ramp * i / max(1, args.calls))
    return await one_call(i, args, ws_url, origin, audio)

  results = await asyncio.gather(*(delayed(i) for i in range(args.calls)))
  failed = [r for r in results if r.error]
  answered = [r for r in results if r.answer_ms is not None]

  print(f"\nFinished in {time.monotonic() - started:.0f} s")
  print(f"  calls connected : {sum(r.connect_ms is not None for r in results)}/{args.calls}")
  print(f"  calls answered  : {len(answered)}/{args.calls}")
  print(f"  calls failed    : {len(failed)}")
  for reason, count in Counter(r.error for r in failed).most_common():
    print(f"      {count} x {reason}")
  print(f"  connect         : {_stats([r.connect_ms for r in results if r.connect_ms is not None])}")
  print(f"  greeting        : {_stats([r.greeting_ms for r in results if r.greeting_ms is not None])}")
  print(f"  answer          : {_stats([r.answer_ms for r in answered])}")

  answer_p95 = sorted(r.answer_ms for r in answered)[int(round(0.95 * (len(answered) - 1)))] if answered else None
  passed = not failed and answer_p95 is not None and answer_p95 <= args.max_p95_answer_ms
  print(f"\nRESULT: {'PASS' if passed else 'FAIL'} (no failed calls, answer p95 <= {args.max_p95_answer_ms:.0f} ms)")
  return 0 if passed else 1


if __name__ == "__main__":
  sys.exit(asyncio.run(main()))
