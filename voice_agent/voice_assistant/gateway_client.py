"""HTTP client for the Maxis agent gateway (roaming.yaml, messages.yaml).

GATEWAY_URL decides where requests go:
  - "mock" (default): the in-process mock gateway with DEMO DATA (mock_gateway/);
  - http://127.0.0.1:8090: the standalone mock (`python -m mock_gateway`);
  - https://<real gateway>: Maxis's gateway on Cloud Run. Requests carry a Google
    ID token for that URL (run.invoker), as CX Agent Studio does today.

The calling number is sent only as the X-Caller-Id header, taken from session
state by the tools; the model never sees or sets it. X-Conversation-Id carries
the session ID (the Genesys conversation ID once telephony is connected).
Network errors and 5xx answers come back as the gateway's own fail envelope
(BACKEND_UNAVAILABLE), so the agent's "retry once, then offer a person" rule applies.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

import httpx

from streaming_core.utils import logger

_client: httpx.AsyncClient | None = None
_token: tuple[str, float] | None = None   # (ID token, expiry epoch seconds)


def gateway_url() -> str:
  return os.getenv("GATEWAY_URL", "mock").strip() or "mock"


def is_mock() -> bool:
  return gateway_url() == "mock"


def _get_client() -> httpx.AsyncClient:
  global _client
  if _client is None:
    timeout = float(os.getenv("GATEWAY_TIMEOUT_S", "30"))  # buyPass can wait ~20 s for roaming
    if is_mock():
      from mock_gateway import httpx_transport
      _client = httpx.AsyncClient(base_url="http://mock-gateway.local", transport=httpx_transport(),
                                  timeout=timeout)
    else:
      _client = httpx.AsyncClient(base_url=gateway_url().rstrip("/"), timeout=timeout)
  return _client


def _use_id_token() -> bool:
  mode = os.getenv("GATEWAY_AUTH", "auto").lower()
  if mode == "auto":
    return gateway_url().startswith("https://")
  return mode == "id_token"


async def _auth_header() -> dict[str, str]:
  """Google ID token for the gateway URL (audience), cached until 5 min before expiry."""
  global _token
  if not _use_id_token():
    return {}
  if _token is None or time.time() > _token[1] - 300:
    import google.auth.transport.requests
    import google.oauth2.id_token
    audience = os.getenv("GATEWAY_AUDIENCE") or gateway_url().rstrip("/")
    token = await asyncio.to_thread(
        google.oauth2.id_token.fetch_id_token, google.auth.transport.requests.Request(), audience)
    _token = (token, time.time() + 3600)
  return {"Authorization": f"Bearer {_token[0]}"}


def _fail(code: str, message: str, action: str) -> dict:
  return {"status": "fail", "message": message,
          "violations": [{"code": code, "message": message, "action": action}],
          "timeStamp": datetime.now(timezone.utc).isoformat()}


async def request(method: str, path: str, *, caller_id: str | None, conversation_id: str | None,
                  params: dict | None = None, body: dict | None = None) -> dict:
  """Calls one gateway operation and returns its JSON envelope (never raises)."""
  headers = {}
  if caller_id:
    headers["X-Caller-Id"] = caller_id
  if conversation_id:
    headers["X-Conversation-Id"] = conversation_id
  params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
  body = {k: v for k, v in (body or {}).items() if v not in (None, "")} if body is not None else None
  try:
    headers.update(await _auth_header())
    resp = await _get_client().request(method, path, params=params or None, json=body, headers=headers)
  except Exception as e:  # network, DNS, timeout, token
    logger.warning("Gateway %s %s failed: %s", method, path, type(e).__name__)
    return _fail("BACKEND_UNAVAILABLE", "The roaming system didn't answer.", "apologise_and_offer_agent")
  if resp.status_code >= 500 or resp.status_code in (401, 403):
    logger.warning("Gateway %s %s -> HTTP %s", method, path, resp.status_code)
    return _fail("BACKEND_UNAVAILABLE", "The roaming system returned an error.", "apologise_and_offer_agent")
  try:
    data = resp.json()
  except ValueError:
    return _fail("BACKEND_UNAVAILABLE", "The roaming system sent an unreadable answer.",
                 "apologise_and_offer_agent")
  if resp.status_code >= 400 and data.get("status") != "fail":
    return _fail("INVALID_REQUEST", str(data.get("message") or "The request was not accepted."),
                 "fix_the_request")
  return data
