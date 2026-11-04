"""Client for the Maxis agent gateway, shared by every flow's tools.

Tools call `call_gateway(operation_id, method, path, tool_context, query=..., body=...)`
with the OpenAPI operationId, method and path of the endpoint (see the gw_* specs).

Every request is a real HTTPS call to GATEWAY_URL (staging or prod) with:
  Authorization: Bearer <Google ID token, audience = GATEWAY_URL>  (Cloud Run IAM)
  X-Caller-Id: <MSISDN, digits only>   from session state "msisdn" / "caller_id"
  X-Conversation-Id: <session id>      end-to-end tracing
  Accept / Content-Type: application/json
The MSISDN is never a tool parameter, so the model can't see, choose or change it.

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

from .auth import get_id_token
from .envelope import fail
from .safe_tool import internal_error

logger = logging.getLogger("voice_agent.gateway")

# TEMPORARY: GET only while testing on the PROD gateway (real lines). Restore
# {"GET", "POST"} together with the commented-out POST tool calls.
_ALLOWED_METHODS = {"GET"}
# Digits only (E.164 without "+"): also blocks header injection.
MSISDN_RE = re.compile(r"[0-9]{8,15}")
CONVERSATION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DEFAULT_TIMEOUT_S = 15.0
_http: requests.Session | None = None  # reused: keeps TLS connections warm


def _compact(params: dict | None) -> dict:
  """Drops unset values so optional parameters are simply left out."""
  return {k: v for k, v in (params or {}).items() if v is not None and v != ""}


def _is_safe_base_url(url: str) -> bool:
  parsed = urlparse(url)
  if parsed.scheme == "https" and parsed.hostname:
    return True
  return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}


def gateway_url() -> str:
  return os.getenv("GATEWAY_URL", "").strip().rstrip("/")


def _timeout() -> float:
  try:
    return float(os.getenv("GATEWAY_TIMEOUT_S", _DEFAULT_TIMEOUT_S))
  except ValueError:
    return _DEFAULT_TIMEOUT_S


def build_headers(base_url: str, msisdn: str, conversation_id: str, *, json_body: bool) -> dict:
  """Headers for one gateway request. Secrets and the MSISDN are never logged."""
  headers = {"Accept": "application/json", "X-Caller-Id": msisdn}
  if json_body:
    headers["Content-Type"] = "application/json"
  if CONVERSATION_ID_RE.fullmatch(conversation_id or ""):
    headers["X-Conversation-Id"] = conversation_id
  # The Maxis gateway is a Cloud Run service behind IAM: it needs a Google-signed
  # ID token whose audience is the gateway URL (GATEWAY_AUTH=google_id_token).
  if os.getenv("GATEWAY_AUTH", "google_id_token").strip().lower() == "google_id_token":
    headers["Authorization"] = "Bearer " + get_id_token(base_url)
  api_key = os.getenv("GATEWAY_API_KEY", "").strip()
  if api_key:  # only for gateways that also want an API key
    headers["x-api-key"] = api_key
  return headers


def _http_call(base_url: str, method: str, path: str, msisdn: str, conversation_id: str,
               query: dict, body: dict) -> dict:
  global _http
  headers = build_headers(base_url, msisdn, conversation_id, json_body=(method == "POST"))
  if _http is None:
    _http = requests.Session()
  resp = _http.request(
      method, base_url + path,
      params=query or None,
      json=body if method == "POST" else None,
      headers=headers,
      timeout=_timeout(),
      allow_redirects=False,
  )
  try:
    data = resp.json()
  except ValueError:
    data = None
  # The gateway answers business errors (4xx) with the same envelope: pass those through.
  if isinstance(data, dict) and data.get("status") in ("success", "fail"):
    return data
  # Anything else (403 IAM, 404 path, 5xx, HTML) is a platform problem, not a caller one.
  raise RuntimeError(f"unexpected gateway response (HTTP {resp.status_code})")


async def call_gateway(operation_id: str, method: str, path: str, tool_context, *,
                       query: dict | None = None, body: dict | None = None) -> dict:
  """Calls one gateway operation and returns its response envelope (never raises)."""
  method = method.upper()
  if method not in _ALLOWED_METHODS:
    raise ValueError(f"Unsupported method {method}")  # programming error, not caller input
  query, body = _compact(query), _compact(body)
  msisdn, conversation_id = call_identity(tool_context)
  base_url = gateway_url()
  started = time.perf_counter()
  if not base_url or not _is_safe_base_url(base_url):
    logger.error("GATEWAY_URL is missing or not https; can't call %s.", operation_id)
    result = internal_error()
  elif not MSISDN_RE.fullmatch(msisdn):
    logger.warning("Gateway %s skipped: no valid caller number in session state.", operation_id)
    result = fail("NOT_A_MAXIS_NUMBER", "The calling number isn't available.", "general_help_only")
  else:
    try:
      result = await asyncio.to_thread(
          _http_call, base_url, method, path, msisdn, conversation_id, query, body)
    except Exception as e:  # network errors, timeouts, token errors, bad responses
      # RuntimeError text carries only the HTTP status (e.g. 403 = missing run.invoker).
      logger.error("Gateway %s failed: %s %s", operation_id, type(e).__name__,
                   e if isinstance(e, RuntimeError) else "")
      result = internal_error()
  violation = (result.get("violations") or [{}])[0].get("code", "")
  if violation == "BACKEND_UNAVAILABLE":
    result = internal_error()  # Maxis backend down: technical, not a business answer
  next_step = (result.get("responseData") or {}).get("next_step", "")
  # Logs carry no MSISDN or payloads (PII); just the operation, outcome and latency.
  logger.info("Gateway %s -> %s %s (%.0f ms) conv=%s", operation_id, result.get("status"),
              violation or next_step, (time.perf_counter() - started) * 1000, conversation_id)
  result.pop("timeStamp", None)  # not useful to the model; saves tokens
  return result


def call_identity(tool_context) -> tuple[str, str]:
  """(msisdn, conversation_id) for this call, from session state set at session start.

  They are never tool parameters, so the model can't see, choose or change them.
  """
  state = getattr(tool_context, "state", None)
  if state is None:
    return "", ""
  # "msisdn" is set by streaming_core.call; "caller_id" is the same number under the
  # name the gateway spec uses (x-ces-session-context: $context.variables.caller_id).
  msisdn = state.get("msisdn") or state.get("caller_id") or ""
  return str(msisdn), str(state.get("conversation_id") or "")
