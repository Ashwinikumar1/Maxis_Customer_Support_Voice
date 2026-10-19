"""Small helpers (ported from streaming_service.py)."""

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("voice_agent")

SEND_SAMPLE_RATE = 16000     # client -> Gemini (PCM16 mono)
RECEIVE_SAMPLE_RATE = 24000  # Gemini -> client (PCM16 mono)


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
