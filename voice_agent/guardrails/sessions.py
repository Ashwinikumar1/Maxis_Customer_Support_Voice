"""Session store factory (baseline AG-07).

InMemorySessionService loses every call's state (verification level, confirmation tokens,
injection flag) when a Cloud Run instance restarts or scales in, and cannot be audited. Set
SESSION_DB_URL to use ADK's DatabaseSessionService. The URL must name an async driver, as ADK's
session docs require: `postgresql+asyncpg://...` for PostgreSQL, `sqlite+aiosqlite:///...` for
SQLite. Both need the `google-adk[db]` extra (and `asyncpg`), which pyproject.toml includes.

For Cloud SQL, use one of the connection methods Cloud SQL documents for IAM database
authentication: the Cloud SQL Auth Proxy (as a Cloud Run sidecar with `--auto-iam-authn`, URL
`postgresql+asyncpg://SA_NAME%40PROJECT.iam@127.0.0.1:5432/DB`) or the Cloud SQL Python
Connector. Otherwise use a database password kept in Secret Manager (deploy.sh mounts
SESSION_DB_URL from SESSION_DB_URL_SECRET). Passwordless IAM login through Cloud Run's built-in
/cloudsql socket is not documented, so it isn't relied on here.

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
