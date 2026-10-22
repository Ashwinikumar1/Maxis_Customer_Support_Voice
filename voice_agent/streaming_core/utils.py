"""Small helpers: audio sample rates, logger, timezone-aware date info."""

import logging
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("voice_agent")

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
