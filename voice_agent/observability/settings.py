"""Environment-driven settings for the observability add-on.

Read lazily (functions, not constants) so values loaded from .env by the
launcher, or set by tests, are always honoured.
"""

import os

_TRUE = {"1", "true", "yes", "on"}


def flag(name: str, default: bool = True) -> bool:
  value = os.getenv(name)
  return default if value is None or not value.strip() else value.strip().lower() in _TRUE


def log_level() -> str:
  return (os.getenv("LOG_LEVEL") or "INFO").strip().upper()


def log_format() -> str:
  """'json' on Cloud Run (K_SERVICE is set there), 'text' locally, unless LOG_FORMAT says otherwise."""
  value = (os.getenv("LOG_FORMAT") or "").strip().lower()
  if value in {"json", "text"}:
    return value
  return "json" if os.getenv("K_SERVICE") else "text"


def project_id() -> str | None:
  return os.getenv("GOOGLE_CLOUD_PROJECT") or None


def shutdown_flush_after_s() -> float:
  """Seconds after SIGTERM at which telemetry is force-flushed even if cleanup is still running.

  Cloud Run sends SIGKILL 10 s after SIGTERM, so the default leaves ~3 s for the flush.
  """
  try:
    return float(os.getenv("OBS_SHUTDOWN_FLUSH_AFTER_S", "7"))
  except ValueError:
    return 7.0


# Safe defaults applied before ADK is imported. setdefault: an explicit value always wins.
PRIVACY_DEFAULTS = {
    # ADK defaults this to TRUE when unset, which copies caller speech into spans.
    "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "false",
    # Explicit "no message content" for the GenAI semantic-convention path.
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT",
}
