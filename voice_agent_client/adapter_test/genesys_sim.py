"""Fake Genesys Cloud Audio Connector: calls the ces-genesys-adapter exactly like Genesys does,
and records the whole call so you can listen to it.

- WebSocket with X-API-KEY + HTTP Message Signature (HMAC-SHA256 over @request-target,
  @authority, x-api-key): what the adapter's auth.verify_request checks.
- open (inputVariables _agent_id + caller_id, PCMU 8 kHz) -> expects opened.
- Streams 20 ms PCMU frames continuously, like a phone line. Each --wav is one caller turn,
  spoken after the bot has finished its reply (bot silent for --gap seconds).
- Bot disconnect -> sends close (like Genesys); after the last turn it hangs up itself.
- Writes --out: a stereo WAV (left = caller, right = bot) on the call's timeline, and
  --out with "_mix" (mono) for laptop speakers.

  say -o /tmp/q1.wav --data-format=LEI16@16000 "How much is the roaming pass for Japan?"
  python genesys_sim.py --url ws://127.0.0.1:8091/ --wav /tmp/q1.wav --out call.wav

env: GENESYS_API_KEY, GENESYS_CLIENT_SECRET (base64): the adapter's credentials.
"""
import argparse
import asyncio
import audioop  # audioop-lts (installed with the adapter)
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
import wave
from urllib.parse import urlparse

from websockets.asyncio.client import connect

RATE, FRAME = 8000, 160
SILENCE = b"\xff" * FRAME


def signed_headers(url: str) -> dict:
  api_key, secret = os.environ["GENESYS_API_KEY"], base64.b64decode(os.environ["GENESYS_CLIENT_SECRET"])
  u = urlparse(url)
  comps = '"@request-target" "@authority" "x-api-key"'
  params = f'created={int(time.time())};keyid="test";nonce="{uuid.uuid4().hex}";alg="hmac-sha256"'
  base = "\n".join([f'"@request-target": {u.path or "/"}', f'"@authority": {u.netloc}',
                    f'"x-api-key": {api_key}', f'"@signature-params": ({comps});{params}'])
  sig = base64.b64encode(hmac.new(secret, base.encode(), hashlib.sha256).digest()).decode()
  headers = {"X-API-KEY": api_key, "Signature-Input": f"sig1=({comps});{params}", "Signature": f"sig1=:{sig}:"}
  if os.getenv("CLOUD_RUN_ID_TOKEN"):
    headers["Authorization"] = f"Bearer {os.environ['CLOUD_RUN_ID_TOKEN']}"
  return headers


def wav_to_ulaw(path: str) -> bytes:
  with wave.open(path, "rb") as w:
    if w.getsampwidth() != 2:
      raise SystemExit(f"{path}: WAV must be 16-bit PCM")
    pcm, rate, ch = w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()
  if ch == 2:
    pcm = audioop.tomono(pcm, 2, 1, 0)
  pcm, _ = audioop.ratecv(pcm, 2, 1, rate, RATE, None)
  return audioop.lin2ulaw(pcm, 2)


def save(path: str, caller: bytearray, bot: bytearray):
  n = max(len(caller), len(bot))
  c = audioop.ulaw2lin(bytes(caller.ljust(n, b"\xff")), 2)
  b = audioop.ulaw2lin(bytes(bot.ljust(n, b"\xff")), 2)
  stereo = audioop.add(audioop.tostereo(c, 2, 1, 0), audioop.tostereo(b, 2, 0, 1), 2)
  mix = audioop.add(c, b, 2)
  root, ext = os.path.splitext(path)
  for p, data, chans in ((path, stereo, 2), (f"{root}_mix{ext or '.wav'}", mix, 1)):
    with wave.open(p, "wb") as w:
      w.setnchannels(chans); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(data)
  return f"{root}_mix{ext or '.wav'}"


async def main(a):
  sid = str(uuid.uuid4())
  conv = "00000000-0000-0000-0000-000000000000" if a.scenario == "probe" else str(uuid.uuid4())
  turns = [wav_to_ulaw(p) for p in a.wav]
  t0 = time.monotonic()
  seq = {"n": 0, "server": 0}
  caller_track, bot_track = bytearray(), bytearray()
  st = {"bot_end": 0, "bot_spoke": False, "disconnect": None, "first_audio": None}

  def now_s() -> float:
    return time.monotonic() - t0

  def log(*x):
    print(f"[genesys {now_s():6.2f}s]", *x, flush=True)

  async with connect(a.url, additional_headers=signed_headers(a.url), proxy=None, max_size=1 << 20) as ws:
    async def send(kind, parameters=None):
      seq["n"] += 1
      await ws.send(json.dumps({"version": "2", "type": kind, "seq": seq["n"], "serverseq": seq["server"],
                                "id": sid, "position": "PT0S", "parameters": parameters or {}}))

    await send("open", {
        "organizationId": str(uuid.uuid4()), "conversationId": conv,
        "participant": {"id": str(uuid.uuid4()), "ani": f"+{a.caller}", "aniName": "", "dnis": "+60312345678"},
        "media": [{"type": "audio", "format": "PCMU", "channels": ["external"], "rate": RATE}],
        "language": "en-US",
        "inputVariables": {"_agent_id": "projects/mxs-maxisapp-prd/locations/us/apps/adk",
                           "caller_id": f"+{a.caller}"}})
    first = json.loads(await asyncio.wait_for(ws.recv(), 15))
    log("<-", first["type"], first.get("parameters"))
    if first["type"] != "opened" or a.scenario == "probe":
      st["disconnect"] = first.get("parameters") if first["type"] == "disconnect" else None
      return st
    log(f"call {conv} opened, caller +{a.caller}")

    async def caller() -> str:
      queue, speech, nxt = list(turns), b"", time.monotonic()
      while True:
        bot_quiet = st["bot_spoke"] and len(caller_track) > st["bot_end"] + int(a.gap * RATE)
        if not speech and bot_quiet:
          if not queue:
            await asyncio.sleep(a.tail)
            return "caller done"
          speech = queue.pop(0)
          st["bot_spoke"] = False
          log(f"-> caller turn {len(turns) - len(queue)}/{len(turns)} ({len(speech) / RATE:.1f}s)")
        if a.scenario == "hangup" and st["bot_spoke"] and now_s() > (st["first_audio"] or 0) + 1.0:
          return "caller hung up mid-greeting"
        frame, speech = (speech[:FRAME], speech[FRAME:]) if speech else (SILENCE, b"")
        frame = frame.ljust(FRAME, b"\xff")
        caller_track.extend(frame)
        await ws.send(frame)
        nxt += FRAME / RATE
        await asyncio.sleep(max(0, nxt - time.monotonic()))

    async def receiver() -> str:
      async for msg in ws:
        if isinstance(msg, bytes):
          if st["first_audio"] is None:
            st["first_audio"] = now_s()
            log(f"<- bot audio starts ({now_s():.2f}s after open)")
          # Genesys plays its buffer in order: place each chunk after the previous one
          start = max(len(caller_track), st["bot_end"])
          if len(bot_track) < start:
            bot_track.extend(b"\xff" * (start - len(bot_track)))
          bot_track[start:start + len(msg)] = msg
          st["bot_end"], st["bot_spoke"] = start + len(msg), True
          continue
        m = json.loads(msg)
        seq["server"] = m.get("seq", seq["server"])
        log("<-", m["type"], m.get("parameters", {}))
        if m["type"] == "disconnect":
          st["disconnect"] = m["parameters"]
          await send("close", {"reason": "end"})
        elif m["type"] == "closed":
          return "closed"
      return "socket closed"

    rx, tx = asyncio.create_task(receiver()), asyncio.create_task(caller())
    done, _ = await asyncio.wait({rx, tx}, timeout=a.timeout, return_when=asyncio.FIRST_COMPLETED)
    if tx in done and not rx.done():
      log(f"-> {tx.result()}: close")
      await send("close", {"reason": "end"})
      try:
        await asyncio.wait_for(rx, 10)
      except (asyncio.TimeoutError, Exception):
        pass
    for t in (rx, tx):
      t.cancel()

  mix = save(a.out, caller_track, bot_track)
  log(f"recording: {a.out} (stereo: L caller, R bot) and {mix}; {len(caller_track) / RATE:.1f}s")
  return st


if __name__ == "__main__":
  p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  p.add_argument("--url", default="ws://127.0.0.1:8091/")
  p.add_argument("--wav", action="append", default=[], help="caller turn (repeat for more turns)")
  p.add_argument("--caller", default="60164129768", help="caller number, sent as caller_id")
  p.add_argument("--scenario", choices=["call", "hangup", "probe"], default="call")
  p.add_argument("--gap", type=float, default=1.5, help="bot silence that ends its turn (s)")
  p.add_argument("--tail", type=float, default=4.0, help="silence after the last reply before hanging up")
  p.add_argument("--timeout", type=float, default=180.0)
  p.add_argument("--out", default="call.wav")
  args = p.parse_args()
  if args.scenario == "call" and not args.wav:
    p.error("--wav is required for a call")
  result = asyncio.run(main(args))
  print("RESULT", json.dumps({"scenario": args.scenario, "disconnect": result.get("disconnect"),
                              "first_bot_audio_s": result.get("first_audio")}), flush=True)
