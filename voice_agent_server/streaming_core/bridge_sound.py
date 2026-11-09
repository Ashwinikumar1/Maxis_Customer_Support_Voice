"""Keyboard-clicking sound that fills the silence while the caller waits.

Plays during tool calls (e.g. a gateway lookup) and agent transfers (ADK closes the
Live connection and opens a new one for the next agent: ~5 s of silence), so the
caller knows we're working on it.

It is sent as normal call audio (PCM16 24 kHz, the model's voice format) through the
channel's emit(), so every channel plays it: the browser today, Genesys later (the
Genesys adapter converts all audio to PCMU 8 kHz, so it needs nothing extra).

- Starts on any tool call or transfer_to_agent; stops as soon as an agent speaks, on
  barge-in, when the call ends, or after MAX_S as a safety net.
- Sent at playback speed with at most ~150 ms queued ahead, so the agent's first
  words aren't delayed and stopping is instant (Genesys has no reliable "clear
  buffer", so keeping its buffer short is what makes barge-in work).
- The clip is generated once at import (filtered noise ticks), so there is no audio
  file to ship or license. BRIDGE_SOUND=false turns it off.
"""

import asyncio
import math
import os
import random
import sys
import time
from array import array

from .utils import RECEIVE_SAMPLE_RATE

ENABLED = os.getenv("BRIDGE_SOUND", "true").strip().lower() == "true"
_CHUNK_S = 0.1        # send in 100 ms pieces
_MAX_AHEAD_S = 0.15   # never queue more than this ahead of what the caller hears
_MAX_S = 20.0         # safety stop (a transfer takes ~5 s; slow tools up to ~15 s)
_BYTES_PER_S = RECEIVE_SAMPLE_RATE * 2  # PCM16 mono


def _make_clip(seconds: float = 2.4, volume: float = 0.12) -> bytes:
  """A loopable clip of quiet keyboard clicks (PCM16 mono, 24 kHz)."""
  rng = random.Random(7)  # same clip every run
  n = int(seconds * RECEIVE_SAMPLE_RATE)
  out = [0.0] * n
  t = 0.05
  while t < seconds - 0.05:
    start = int(t * RECEIVE_SAMPLE_RATE)
    length = int(RECEIVE_SAMPLE_RATE * rng.uniform(0.015, 0.03))
    amp = volume * rng.uniform(0.5, 1.0)
    prev = 0.0
    for i in range(min(length, n - start)):
      white = rng.uniform(-1.0, 1.0)
      click = white - prev  # simple high-pass: a bright click, not a hiss
      prev = white
      out[start + i] += amp * click * math.exp(-5.0 * i / length)
    # Fast typing with an occasional pause between "words".
    t += rng.uniform(0.25, 0.55) if rng.random() < 0.12 else rng.uniform(0.07, 0.17)
  pcm = array("h", (max(-32768, min(32767, int(s * 32767))) for s in out))
  if sys.byteorder == "big":
    pcm.byteswap()  # PCM16 little-endian on the wire
  return pcm.tobytes()


_CLIP = _make_clip()  # 2.4 s = 24 chunks of 100 ms, so it loops seamlessly


class BridgeSound:
  """Plays the clicking loop through `emit("audio", data=...)` until stopped."""

  def __init__(self, emit):
    self._emit = emit
    self._task: asyncio.Task | None = None
    self._playhead = 0.0  # monotonic time when the caller's queued audio runs out

  def note_audio(self, nbytes: int):
    """Call for every model audio chunk sent to the caller."""
    now = time.monotonic()
    self._playhead = max(self._playhead, now) + nbytes / _BYTES_PER_S

  def clear(self):
    """The caller's audio queue was flushed (barge-in)."""
    self._playhead = time.monotonic()

  def start(self):
    if ENABLED and self._task is None:
      self._task = asyncio.create_task(self._loop())

  def stop(self):
    if self._task is not None:
      self._task.cancel()
      self._task = None

  async def _loop(self):
    chunk = int(_CHUNK_S * RECEIVE_SAMPLE_RATE) * 2
    pos = 0
    deadline = time.monotonic() + _MAX_S
    try:
      while time.monotonic() < deadline:
        ahead = self._playhead - time.monotonic()
        if ahead > _MAX_AHEAD_S:  # the agent's last words are still playing
          await asyncio.sleep(ahead - _MAX_AHEAD_S)
          continue
        data = _CLIP[pos:pos + chunk]
        pos = (pos + chunk) % len(_CLIP)
        self.note_audio(len(data))
        await self._emit("audio", data=data)
    except asyncio.CancelledError:
      pass
    finally:
      if self._task is asyncio.current_task():
        self._task = None
