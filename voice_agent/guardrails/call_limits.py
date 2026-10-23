"""Ending calls that have gone quiet (baseline AP-11).

Why this exists
---------------
MAX_SESSION_SECONDS in server.py caps how long any call can last. That alone leaves a gap: a
browser tab left open with the microphone on, or a phone line that was never hung up, holds a
Gemini Live session (cost), a Cloud Run slot (capacity) and an open audio channel (risk: anything
said nearby later is still sent to the model) for the full 30 minutes.

This module ends a call once *nobody* has said anything for SILENCE_TIMEOUT_SECONDS
(default 120 s).

What counts as "someone said something"
---------------------------------------
The browser keeps sending microphone frames even when the room is silent, so "a frame arrived"
can't be the test. Activity is any of:

* a caller audio frame loud enough to be speech (its RMS level, see below, is above
  SILENCE_RMS_THRESHOLD, default 500 on the 16-bit scale);
* a typed text message from the caller;
* a transcript of either side (Gemini heard the caller, or is speaking);
* audio from the agent, so a caller listening to a long answer is never cut off.

RMS ("root mean square") is the usual measure of how loud a stretch of audio is: square every
sample, average, take the square root. Room noise on a laptop mic is typically 50 to 300; normal
speech is well above 1000, so 500 separates them with margin. Raise it if background noise keeps
calls open; lower it if quiet speakers get cut off.

How it ends the call
--------------------
`watch()` is an asyncio task that server.py runs next to the upstream and downstream tasks. It
wakes up every few seconds, and when the quiet period is over it simply returns. server.py
already ends the call when *any* of its tasks finishes, so returning is all it takes: the normal
cleanup (close the Live session, save the redacted transcript, close the socket) then runs.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from array import array

DEFAULT_SILENCE_TIMEOUT_SECONDS = 120
DEFAULT_RMS_THRESHOLD = 500
# How often the watchdog looks at the clock. A few seconds of lateness doesn't matter for a
# 2-minute limit, and waking rarely costs nothing.
CHECK_INTERVAL_SECONDS = 5


def pcm16_rms(pcm: bytes) -> float:
  """Loudness of little-endian 16-bit mono PCM (the browser sends 16 kHz PCM16)."""
  if len(pcm) < 2:
    return 0.0
  samples = array("h")
  # An odd trailing byte can't form a sample; drop it rather than fail.
  samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
  if samples.itemsize != 2:  # pragma: no cover - 'h' is 2 bytes on every supported platform
    raise RuntimeError("unexpected sample size")
  # array.frombytes uses the machine's byte order; Cloud Run (x86/ARM) is little-endian like the
  # browser's PCM, so no byteswap is needed.
  return math.sqrt(sum(s * s for s in samples) / len(samples))


class SilenceWatch:
  """Tracks the last moment anyone spoke, and ends the call after a quiet period."""

  def __init__(self, timeout_seconds: float | None = None, rms_threshold: float | None = None,
               clock=time.monotonic):
    self.timeout = float(timeout_seconds if timeout_seconds is not None
                         else os.getenv("SILENCE_TIMEOUT_SECONDS", DEFAULT_SILENCE_TIMEOUT_SECONDS))
    self.threshold = float(rms_threshold if rms_threshold is not None
                           else os.getenv("SILENCE_RMS_THRESHOLD", DEFAULT_RMS_THRESHOLD))
    # monotonic, not wall-clock time: a clock change on the host can't end or extend a call.
    self._clock = clock
    self._last = clock()

  def activity(self) -> None:
    """Someone spoke, typed, or the agent produced output: restart the quiet period."""
    self._last = self._clock()

  def caller_audio(self, pcm: bytes) -> None:
    """Called for every microphone frame; counts only frames loud enough to be speech."""
    if pcm16_rms(pcm) >= self.threshold:
      self.activity()

  def quiet_for(self) -> float:
    return self._clock() - self._last

  async def watch(self) -> str:
    """Runs until the call has been quiet for `timeout` seconds, then returns "silence".

    A timeout of 0 or less disables the limit (the task just waits until it is cancelled when
    the call ends for another reason).
    """
    if self.timeout <= 0:
      await asyncio.Event().wait()  # never set: sleeps until cancelled
    while self.quiet_for() < self.timeout:
      # Sleep until the earliest moment the limit could be reached, but no longer than the check
      # interval, so a reset by activity() is noticed promptly.
      await asyncio.sleep(min(CHECK_INTERVAL_SECONDS, self.timeout - self.quiet_for() + 0.01))
    return "silence"
