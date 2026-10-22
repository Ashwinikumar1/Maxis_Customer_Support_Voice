"""Graceful SIGTERM handling and telemetry flush (gap #4).

Cloud Run sends SIGTERM and kills the container 10 s later. The server only
handles Ctrl-C (KeyboardInterrupt), and as PID 1 with no handler SIGTERM is
ignored until the SIGKILL, so buffered spans, metrics and in-flight
transcripts are lost today.

On SIGTERM we:
1. log the termination (Google's guidance: record when an instance exits);
2. start a watchdog that force-flushes traces, metrics and logs after
   OBS_SHUTDOWN_FLUSH_AFTER_S (default 7 s) even if cleanup is still running;
3. stop the server the way Ctrl-C does - through asyncio's own SIGINT handler
   (see _on_sigterm for why): the main task is cancelled, the server closes
   its connections, the open calls' `finally` blocks end their spans and
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
  # Tell server.py that calls closing from now on were cut by the shutdown, so
  # they are recorded as close_reason="server_shutdown" (see streaming_core/utils.py).
  try:
    from streaming_core.utils import shutdown_requested
    shutdown_requested.set()
  except ImportError:  # the add-on used without this app
    pass
  # Close open calls with WebSocket code 1001 ("going away") - required on
  # Python 3.12, where stopping asyncio's server does not close them (see
  # ws_hooks.close_servers for the details and the measured 10.6 s it caused).
  from .ws_hooks import close_servers
  close_servers()

  # HOW TO STOP SAFELY - do not change this back to a plain `raise KeyboardInterrupt`.
  # Raising from a signal handler interrupts WHATEVER code is running at that
  # instant. Inside the asyncio event loop that can be the middle of the
  # server's connection bookkeeping; about 1 shutdown in 20, the server then
  # waited forever for a connection count that never reached zero, so the
  # process did not exit and Cloud Run had to SIGKILL it after 10 s.
  # (Found by the end-to-end test; reproduced with a stack dump of the hang.)
  #
  # asyncio.run() avoids this for Ctrl-C: asyncio.Runner installs its own
  # SIGINT handler that CANCELS THE MAIN TASK through the event loop, so
  # shutdown runs as ordinary code. Verified in CPython 3.12 and 3.13
  # (asyncio/runners.py: Runner.run and Runner._on_sigint). We hand SIGTERM to
  # that same handler. After the main task finishes cancelling, asyncio.run()
  # raises KeyboardInterrupt, so server.py's existing `except KeyboardInterrupt`
  # still logs "Exiting application...".
  sigint_handler = signal.getsignal(signal.SIGINT)
  if callable(sigint_handler) and sigint_handler is not signal.default_int_handler:
    sigint_handler(signal.SIGINT, frame)
    return
  # No asyncio runner in charge (e.g. SIGTERM before the event loop started).
  raise KeyboardInterrupt


def install() -> None:
  signal.signal(signal.SIGTERM, _on_sigterm)
  # ADK creates its MeterProvider with shutdown_on_exit=False: flush it ourselves.
  atexit.register(flush_all)
