"""Graceful SIGTERM handling and telemetry flush (gap #4).

Cloud Run sends SIGTERM and kills the container 10 s later. The server only
handles Ctrl-C (KeyboardInterrupt), and as PID 1 with no handler SIGTERM is
ignored until the SIGKILL, so buffered spans, metrics and in-flight
transcripts are lost today.

On SIGTERM we:
1. log the termination (Google's guidance: record when an instance exits);
2. start a watchdog that force-flushes traces, metrics and logs after
   OBS_SHUTDOWN_FLUSH_AFTER_S (default 7 s) even if cleanup is still running;
3. raise KeyboardInterrupt, i.e. take the exit path the server already has:
   asyncio cancels the open calls, their `finally` blocks end the spans and
   upload the transcripts, and the launcher flushes again before exiting.
"""

import atexit
import logging
import signal
import threading

from opentelemetry import metrics, trace

from . import settings

logger = logging.getLogger("voice_agent.observability")
_flush_lock = threading.Lock()


def flush_all(timeout_millis: int = 2500) -> None:
  """Best-effort force_flush of every OTel provider that supports it."""
  with _flush_lock:
    providers = [trace.get_tracer_provider(), metrics.get_meter_provider()]
    try:
      from opentelemetry import _logs
      providers.append(_logs.get_logger_provider())
    except Exception:
      pass
    for provider in providers:
      flush = getattr(provider, "force_flush", None)
      if flush is None:
        continue
      try:
        flush(timeout_millis=timeout_millis)
      except Exception:
        logger.warning("Telemetry flush failed for %s", type(provider).__name__)


def _on_sigterm(signum, frame):
  logger.warning("SIGTERM received: shutting down, closing open calls and flushing telemetry")
  watchdog = threading.Timer(settings.shutdown_flush_after_s(), flush_all)
  watchdog.daemon = True
  watchdog.start()
  signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a second SIGTERM must not interrupt cleanup
  raise KeyboardInterrupt


def install() -> None:
  signal.signal(signal.SIGTERM, _on_sigterm)
  # ADK creates its MeterProvider with shutdown_on_exit=False: flush it ourselves.
  atexit.register(flush_all)
