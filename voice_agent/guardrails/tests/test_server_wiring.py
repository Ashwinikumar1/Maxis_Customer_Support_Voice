"""server.py handshake with the IAP check on: proves the wiring, not just the module.

Calls server.process_request with real websockets Request objects. No Gemini connection is made:
everything tested here happens before the WebSocket opens.
"""
import asyncio

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request

from guardrails.identity import IAP_HEADER, IapVerifier
from guardrails.tests.test_identity import AUD, PUBLIC, _token


@pytest.fixture()
def server(monkeypatch):
  monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "x")
  import server as srv

  # Swap in a verifier with local test keys (the real one downloads Google's).
  monkeypatch.setattr(srv, "iap_verifier", IapVerifier(AUD, fetch=lambda: {"k1": PUBLIC}))
  return srv


def _request(path="/ws", origin="http://127.0.0.1:8080", token=None) -> Request:
  headers = Headers([("Host", "127.0.0.1:8080"), ("Origin", origin)])
  if token:
    headers[IAP_HEADER] = token
  return Request(path, headers)


def _status(server, request):
  response = asyncio.run(server.process_request(None, request))
  return None if response is None else response.status_code


def test_valid_iap_header_continues_the_handshake(server):
  assert _status(server, _request(token=_token())) is None  # None = go on to the WebSocket


def test_missing_or_forged_header_gets_401(server):
  assert _status(server, _request()) == 401
  assert _status(server, _request(token=_token(aud="/projects/1/global/backendServices/2"))) == 401


def test_origin_still_checked_first(server):
  assert _status(server, _request(origin="https://evil.example", token=_token())) == 403


def test_ui_files_unaffected(server):
  assert _status(server, _request(path="/")) == 200
  assert _status(server, _request(path="/../server.py")) == 404


def test_server_logs_hash_not_raw_session_ids():
  # Every log line about a session uses session_ref; the raw session_id only goes to ADK and
  # the transcript (whose file name the existing logger builds from it).
  import re
  from pathlib import Path

  src = (Path(__file__).resolve().parents[2] / "server.py").read_text()
  assert not re.search(r"logger\.\w+\([^)]*\bsession_id\b", src)
  assert not re.search(r"logger\.\w+\([^)]*\buser_id\b", src)
