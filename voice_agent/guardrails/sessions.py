"""Session store factory (baseline AG-07).

InMemorySessionService loses every call's state (verification level, confirmation tokens,
injection flag) when a Cloud Run instance restarts or scales in, and cannot be audited. Set
SESSION_DB_URL to use ADK's DatabaseSessionService (for example Cloud SQL for PostgreSQL:
postgresql+asyncpg://USER@/DB?host=/cloudsql/PROJECT:REGION:INSTANCE, with IAM auth).

REQUIRE_PERSISTENT_SESSIONS=true (set it in production) makes startup fail rather than fall
back to memory.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("voice_agent.guardrails")


def make_session_service():
  url = os.getenv("SESSION_DB_URL", "").strip()
  if url:
    from google.adk.sessions import DatabaseSessionService
    return DatabaseSessionService(db_url=url)
  if os.getenv("REQUIRE_PERSISTENT_SESSIONS", "false").lower() == "true":
    raise RuntimeError("REQUIRE_PERSISTENT_SESSIONS is true but SESSION_DB_URL is not set")
  from google.adk.sessions import InMemorySessionService
  logger.warning("Using InMemorySessionService: call state is lost on restart (set SESSION_DB_URL)")
  return InMemorySessionService()
