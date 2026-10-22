import pytest
from google.adk.sessions import InMemorySessionService

from guardrails.sessions import make_session_service


def test_memory_only_in_development(monkeypatch):
  monkeypatch.delenv("SESSION_DB_URL", raising=False)
  monkeypatch.delenv("REQUIRE_PERSISTENT_SESSIONS", raising=False)
  assert isinstance(make_session_service(), InMemorySessionService)


def test_production_refuses_memory(monkeypatch):
  monkeypatch.delenv("SESSION_DB_URL", raising=False)
  monkeypatch.setenv("REQUIRE_PERSISTENT_SESSIONS", "true")
  with pytest.raises(RuntimeError):
    make_session_service()


def test_database_url_gives_persistent_store(monkeypatch, tmp_path):
  # Needs google-adk[db] (SQLAlchemy) and a driver; only installed where SESSION_DB_URL is used.
  pytest.importorskip("sqlalchemy")
  pytest.importorskip("aiosqlite")
  from google.adk.sessions import DatabaseSessionService

  monkeypatch.setenv("SESSION_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/sessions.db")
  assert isinstance(make_session_service(), DatabaseSessionService)
