"""Installs every add-on hook, in the order the existing code needs.

Order matters:
1. .env and safe env defaults BEFORE google.adk / google.genai are imported
   (they read their settings at import time, as server.py notes).
2. Logging BEFORE streaming_core is imported, so its basicConfig() is a no-op.
3. Hooks that wrap module attributes (ADK provider setup, websockets.serve,
   the FAQ search helper) BEFORE server.py binds or calls them.
4. SIGTERM handler last, in the main thread.

Each hook can be switched off with an OBS_* variable (default on).
"""

import logging
import os
from pathlib import Path

from . import settings

logger = logging.getLogger("voice_agent.observability")

APP_DIR = Path(__file__).resolve().parent.parent  # voice_agent/


def _load_env() -> None:
  from dotenv import load_dotenv

  load_dotenv(APP_DIR / ".env")  # same file server.py loads; existing values win
  import certifi

  os.environ.setdefault("SSL_CERT_FILE", certifi.where())  # mirrors server.py, needed before ADK import
  for key, value in settings.PRIVACY_DEFAULTS.items():
    os.environ.setdefault(key, value)


def install() -> None:
  import sys

  if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))  # voice_assistant / streaming_core live next to server.py
  _load_env()

  from . import logs
  logs.configure_logging()

  hooks = [
      ("OBS_SESSION_SPANS", "adk_hook"),
      ("OBS_WS_SECURITY_LOGS", "ws_hooks"),
      ("OBS_FAQ_TRACING", "faq_tracing"),
      ("OBS_SIGTERM", "shutdown"),
  ]
  enabled = []
  for env_name, module_name in hooks:
    if not settings.flag(env_name):
      continue
    try:
      module = __import__(f"{__name__.rsplit('.', 1)[0]}.{module_name}", fromlist=["install"])
      module.install()
      enabled.append(module_name)
    except Exception:
      # A hook that cannot attach (e.g. an ADK upgrade renamed something) must not stop the server.
      logger.exception("Observability hook %s could not be installed; continuing without it", module_name)
  # [#13] The active trace sampler is logged at start-up, so anyone looking at
  # a gap in Cloud Trace can confirm from the logs whether sampling was on.
  logger.info("Observability add-on installed (log_format=%s, level=%s, hooks=%s, trace_sampler=%s%s)",
              settings.log_format(), settings.log_level(), ",".join(enabled) or "none",
              os.getenv("OTEL_TRACES_SAMPLER", "parentbased_always_on (SDK default)"),
              f" {os.getenv('OTEL_TRACES_SAMPLER_ARG')}" if os.getenv("OTEL_TRACES_SAMPLER_ARG") else "")
