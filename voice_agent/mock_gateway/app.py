"""HTTP surface of the mock gateway: the same paths, methods, headers and bodies
as roaming.yaml and messages.yaml.

Two ways to serve it:
  - in-process: `httpx_transport()` plugs into httpx, so the agent's gateway
    client makes real HTTP requests that never leave the process
    (GATEWAY_URL=mock, the default for demos and Cloud Run test builds);
  - standalone: `python -m mock_gateway --port 8090`, then
    GATEWAY_URL=http://127.0.0.1:8090 (to test the HTTP path end to end).

Admin routes (mock only): POST /__mock/reset {"msisdn"?}, POST /__mock/fail {"operation", "count"}.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .service import MockGateway, call

_gateway: MockGateway | None = None


def gateway() -> MockGateway:
  """The single in-memory gateway for this process."""
  global _gateway
  if _gateway is None:
    _gateway = MockGateway()
  return _gateway


def _bool(value: Any) -> bool:
  if isinstance(value, bool):
    return value
  return str(value).lower() in ("true", "1", "yes")


async def handle(method: str, url: str, headers: dict[str, str], body: bytes) -> tuple[int, dict]:
  """One HTTP request -> (status code, JSON body)."""
  parts = urlsplit(url)
  path = parts.path.rstrip("/") or "/"
  query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
  lower = {k.lower(): v for k, v in headers.items()}
  caller = lower.get("x-caller-id")
  conversation = lower.get("x-conversation-id")
  try:
    payload = json.loads(body) if body else {}
  except json.JSONDecodeError:
    return 400, {"status": "fail", "message": "Body must be JSON."}
  if not isinstance(payload, dict):
    return 400, {"status": "fail", "message": "Body must be a JSON object."}
  gw = gateway()

  route = (method.upper(), path)
  if route == ("GET", "/roaming/lines"):
    return 200, await call(gw, "list_roaming_lines", caller_id=caller)
  if route == ("GET", "/roaming/passes"):
    return 200, await call(gw, "find_passes", caller_id=caller, destinations=query.get("destinations", ""),
                           start_date=query.get("start_date"), end_date=query.get("end_date"),
                           days=query.get("days"), line_ref=query.get("line_ref"))
  if route == ("POST", "/roaming/purchases"):
    return 200, await call(gw, "buy_pass", caller_id=caller, pass_id=payload.get("pass_id"),
                           destinations=payload.get("destinations", ""), start_date=payload.get("start_date"),
                           line_ref=payload.get("line_ref"), confirmed=_bool(payload.get("confirmed", False)))
  if route == ("GET", "/roaming/purchases"):
    return 200, await call(gw, "list_my_passes", caller_id=caller, when=query.get("when", "all"),
                           line_ref=query.get("line_ref"))
  if route == ("POST", "/roaming/wrong-pass"):
    return 200, await call(gw, "help_with_wrong_pass", caller_id=caller, pass_ref=payload.get("pass_ref"),
                           line_ref=payload.get("line_ref"))
  if route == ("POST", "/roaming/switch"):
    return 200, await call(gw, "switch_roaming", caller_id=caller, turn=payload.get("turn"),
                           line_ref=payload.get("line_ref"), confirmed=_bool(payload.get("confirmed", False)),
                           acknowledged_active_pass=_bool(payload.get("acknowledged_active_pass", False)))
  if route == ("POST", "/messages/whatsapp"):
    return 200, await call(gw, "send_whatsapp_link", caller_id=caller, kind=payload.get("kind"),
                           conversation_id=conversation)
  if route == ("POST", "/__mock/reset"):
    gw.reset(payload.get("msisdn"))
    return 200, {"status": "success"}
  if route == ("POST", "/__mock/fail"):
    gw.fail_next(payload.get("operation", "*"), payload.get("count", 1))
    return 200, {"status": "success"}
  return 404, {"status": "fail", "message": f"No route {method} {path}"}


def httpx_transport():
  """An httpx transport that answers requests with this mock, in-process."""
  import httpx

  async def _handler(request: httpx.Request) -> httpx.Response:
    status, data = await handle(request.method, str(request.url), dict(request.headers), request.content)
    return httpx.Response(status, json=data)

  return httpx.MockTransport(_handler)
