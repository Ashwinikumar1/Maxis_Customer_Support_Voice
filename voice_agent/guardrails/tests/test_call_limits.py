"""Silence limit (guardrails/call_limits.py), driven by a fake clock so tests take milliseconds."""
import asyncio
from array import array

from guardrails import call_limits
from guardrails.call_limits import SilenceWatch, pcm16_rms


def _pcm(level: int, n: int = 320) -> bytes:
  """n samples alternating +level/-level: RMS equals level."""
  return array("h", [level if i % 2 else -level for i in range(n)]).tobytes()


class Clock:
  def __init__(self):
    self.t = 0.0

  def __call__(self):
    return self.t


def test_rms():
  assert pcm16_rms(b"") == 0.0
  assert pcm16_rms(_pcm(0)) == 0.0
  assert round(pcm16_rms(_pcm(1000))) == 1000
  assert pcm16_rms(_pcm(1000) + b"\x01") > 0  # odd trailing byte ignored, not an error


def test_quiet_frames_do_not_count_loud_ones_do():
  clock = Clock()
  w = SilenceWatch(timeout_seconds=120, rms_threshold=500, clock=clock)
  clock.t = 100
  w.caller_audio(_pcm(200))  # room noise
  assert w.quiet_for() == 100
  w.caller_audio(_pcm(2000))  # speech
  assert w.quiet_for() == 0


def test_watch_returns_after_the_quiet_period(monkeypatch):
  monkeypatch.setattr(call_limits, "CHECK_INTERVAL_SECONDS", 0.01)
  clock = Clock()
  w = SilenceWatch(timeout_seconds=120, rms_threshold=500, clock=clock)

  async def scenario():
    task = asyncio.create_task(w.watch())
    clock.t = 60
    await asyncio.sleep(0.05)
    assert not task.done()      # only 60 s quiet
    w.activity()                # agent spoke: the period restarts
    clock.t = 170
    await asyncio.sleep(0.05)
    assert not task.done()      # 110 s since the agent spoke
    clock.t = 181
    return await asyncio.wait_for(task, 1)

  assert asyncio.run(scenario()) == "silence"


def test_zero_timeout_disables_the_limit():
  w = SilenceWatch(timeout_seconds=0)

  async def scenario():
    task = asyncio.create_task(w.watch())
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()

  asyncio.run(scenario())


def test_settings_from_env(monkeypatch):
  monkeypatch.setenv("SILENCE_TIMEOUT_SECONDS", "45")
  monkeypatch.setenv("SILENCE_RMS_THRESHOLD", "800")
  w = SilenceWatch()
  assert (w.timeout, w.threshold) == (45, 800)
