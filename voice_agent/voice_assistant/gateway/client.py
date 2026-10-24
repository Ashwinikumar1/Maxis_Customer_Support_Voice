"""Reusable client for the Maxis API gateway, shared by every flow's tools.

Tools call `call_gateway(operation_id, method, path, tool_context, query=..., body=...)`
with the OpenAPI operationId, method and path of the endpoint.

- GATEWAY_URL set   -> real HTTPS call. X-Caller-Id comes from session state
                       ("caller_id", set by the server/telephony layer), never from the model.
- GATEWAY_URL empty -> the in-process mock registered for that operationId (dummy data).

Errors never raise into the model: they come back as the gateway's own fail
envelope, so the model simply follows violations[0].action.
"""

import asyncio
import logging
import os
import re
import time
from urllib.parse import urlparse

import requests

from . import mock
from .envelope import fail

logger = logging.getLogger("voice_agent.gateway")

_ALLOWED_METHODS = {"GET", "POST"}
# Digits only (E.164 without "+"): also blocks header injection.
MSISDN_RE = re.compile(r"[0-9]{8,15}")
CONVERSATION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_http: requests.Session | None = None  # reused: keeps TLS connections warm


def _compact(params: dict | None) -> dict:
  """Drops unset values so optional parameters are simply left out."""
  return {k: v for k, v in (params or {}).items() if v is not None and v != ""}


def _is_safe_base_url(url: str) -> bool:
  parsed = urlparse(url)
  if parsed.scheme == "https" and parsed.hostname:
    return True
  return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}


def _http_call(base_url: str, method: str, path: str, msisdn: str, conversation_id: str,
               query: dict, body: dict) -> dict:
  global _http
  if not _is_safe_base_url(base_url):
    logger.error("GATEWAY_URL must use https (http only for localhost); refusing to call it.")
    return fail("BACKEND_UNAVAILABLE", "The service isn't available right now.",
                "apologise_and_offer_agent")
  if not MSISDN_RE.fullmatch(msisdn):
    return fail("NOT_A_MAXIS_NUMBER", "The calling number isn't available.", "general_help_only")
  headers = {"Accept": "application/json", "X-Caller-Id": msisdn}
  if CONVERSATION_ID_RE.fullmatch(conversation_id):
    headers["X-Conversation-Id"] = conversation_id  # end-to-end tracing across the gateway
  # TODO(security): confirm the gateway's auth scheme (API key, Google ID token or mTLS).
  # The key is read from the environment (Secret Manager in prod) and never logged.
  api_key = os.getenv("GATEWAY_API_KEY", "").strip()
  if api_key:
    headers["x-api-key"] = api_key
  if _http is None:
    _http = requests.Session()
  resp = _http.request(
      method, base_url + path,
      params=query if method == "GET" else None,
      json=body if method == "POST" else None,
      headers=headers,
      timeout=float(os.getenv("GATEWAY_TIMEOUT_S", "5")),
      allow_redirects=False,
  )
  try:
    data = resp.json()
  except ValueError:
    data = None
  # The gateway answers business errors (4xx) with the same envelope: pass those through.
  if isinstance(data, dict) and data.get("status") in ("success", "fail"):
    return data
  raise RuntimeError(f"unexpected gateway response (HTTP {resp.status_code})")


async def call_gateway(operation_id: str, method: str, path: str, tool_context, *,
                       query: dict | None = None, body: dict | None = None) -> dict:
  """Calls one gateway operation and returns its response envelope (never raises)."""
  method = method.upper()
  if method not in _ALLOWED_METHODS:
    raise ValueError(f"Unsupported method {method}")  # programming error, not caller input
  query, body = _compact(query), _compact(body)
  msisdn, conversation_id = call_identity(tool_context)
  base_url = os.getenv("GATEWAY_URL", "").strip().rstrip("/")
  started = time.perf_counter()
  try:
    if base_url:
      result = await asyncio.to_thread(
          _http_call, base_url, method, path, msisdn, conversation_id, query, body)
    else:
      ctx = mock.MockContext(msisdn=msisdn or "demo", conversation_id=conversation_id or "local")
      result = mock.dispatch(operation_id, ctx, query, body)
  except Exception as e:  # network errors, timeouts, bad responses
    logger.error("Gateway %s failed: %s", operation_id, type(e).__name__)
    result = fail("BACKEND_UNAVAILABLE", "The service isn't responding right now.",
                  "apologise_and_offer_agent")
  violation = (result.get("violations") or [{}])[0].get("code", "")
  # Logs carry no MSISDN or payloads (PII); just the operation, outcome and latency.
  logger.info("Gateway %s %s -> %s %s (%.0f ms) conv=%s", "http" if base_url else "mock",
              operation_id, result.get("status"), violation,
              (time.perf_counter() - started) * 1000, conversation_id)
  result.pop("timeStamp", None)  # not useful to the model; saves tokens
  return result


def call_identity(tool_context) -> tuple[str, str]:
  """(msisdn, conversation_id) for this call, from session state set at session start.

  They are never tool parameters, so the model can't see, choose or change them.
  """
  state = getattr(tool_context, "state", None)
  if state is None:
    return "", ""
  return str(state.get("msisdn") or ""), str(state.get("conversation_id") or "")
