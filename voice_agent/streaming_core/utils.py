"""Small helpers: audio sample rates, logger, timezone-aware date info."""

import contextvars
import logging
import os
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("voice_agent")


# [Observability #10] Keep the Gemini Live "session resumption handle" out of the logs.
#
# WHAT THE HANDLE IS: Gemini Live connections last about 10 minutes. To keep a
#   longer call going, Gemini sends a resumption handle that lets a NEW
#   connection take over THIS conversation with its context. Google's Live API
#   docs describe it as a token the client sends back to resume the session -
#   in effect a short-lived credential for the conversation.
#   https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/start-manage-session
#
# THE PROBLEM: ADK 2.12 logs every resumption update at INFO level, handle
#   included, from the logger below (verified in google/adk/live/_live_llm_flow.py):
#     Update session resumption handle: new_handle='...' resumable=True ...
#   On Cloud Run every log line goes to Cloud Logging, so anyone who can read
#   the logs could read the handle.
#
# THE FIX: a filter on that one ADK logger replaces the handle with
#   '[redacted]' before the record reaches ANY handler (a logger-level filter
#   runs first). The rest of the line - e.g. resumable=True - stays readable.
#   It lives here, not only in the observability add-on, because server.py
#   imports this module on every start-up path: `python server.py`,
#   `python -m observability`, or any other launcher.
_ADK_LIVE_LOGGER = "google_adk.google.adk.live._live_llm_flow"
_RESUMPTION_HANDLE = re.compile(r"""new_handle=('[^']*'|"[^"]*")""")  # both quote styles Python may print


class _RedactResumptionHandle(logging.Filter):
  redacts_resumption_handle = True  # marker: the add-on checks it to avoid installing a second copy

  def filter(self, record: logging.LogRecord) -> bool:
    try:
      message = record.getMessage()
      if "new_handle=" in message:
        record.msg, record.args = _RESUMPTION_HANDLE.sub("new_handle='[redacted]'", message), None
    except Exception:  # never let log redaction break logging
      pass
    return True  # always keep the record; only its text changes


_adk_live_logger = logging.getLogger(_ADK_LIVE_LOGGER)
if not any(getattr(f, "redacts_resumption_handle", False) for f in _adk_live_logger.filters):
  _adk_live_logger.addFilter(_RedactResumptionHandle())

SEND_SAMPLE_RATE = 16000     # client -> Gemini (PCM16 mono)
RECEIVE_SAMPLE_RATE = 24000  # Gemini -> client (PCM16 mono)

# [Observability #9c] Set once the server has been told to stop (on Cloud Run:
# SIGTERM during a deploy or scale-down; set by observability/shutdown.py).
# WHY IT EXISTS: a clean shutdown closes every open WebSocket from the server
# side, and from inside a call that looks exactly like the caller dropping off.
# server.py checks this flag so such calls are recorded as
# close_reason="server_shutdown", not "client_disconnect" - otherwise deploys
# that cut live calls would be misread as caller or network problems.
shutdown_requested = threading.Event()

# [Observability #13] The session ID of the call the current code is running for.
# WHY IT EXISTS: log lines carry a `session_id` so every line of one call can be
# found together. That ID used to be read from the call's trace span. With
# trace sampling switched on (OTEL_TRACES_SAMPLER, see .env.example), calls
# that are not sampled get a NON-recording span that holds no attributes
# (verified in the OpenTelemetry SDK: Tracer.start_span returns NonRecordingSpan),
# so their log lines would silently lose the session ID. A context variable
# does not depend on sampling: server.py sets it once per call, and asyncio
# copies it into every task the call starts (upstream, model loop, ADK tasks).
current_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_session_id", default=None)


def get_datetime_info(timezone_str: str | None = None) -> dict:
  """Date, time and greeting in the user's timezone (default DEFAULT_TIMEZONE)."""
  try:
    tz = ZoneInfo(timezone_str or os.getenv("DEFAULT_TIMEZONE", "Asia/Kuala_Lumpur"))
  except (ZoneInfoNotFoundError, ValueError):
    tz = ZoneInfo("Asia/Kuala_Lumpur")
  now = datetime.now(tz)
  if 3 <= now.hour < 12:
    greeting = "Good morning"
  elif 12 <= now.hour < 17:
    greeting = "Good afternoon"
  else:
    greeting = "Good evening"
  return {
      "timezone": str(tz),
      "date": now.strftime("%Y-%m-%d"),
      "time": now.strftime("%I:%M %p"),
      "greeting": greeting,
  }
