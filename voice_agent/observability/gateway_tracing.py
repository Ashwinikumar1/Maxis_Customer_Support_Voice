"""Traces the agent gateway calls behind the roaming tools (the FAQ tool's gap #3, for roaming).

Every roaming tool (voice_assistant/roaming_tools.py) turns a failed gateway call into a
polite "fail" envelope for the agent, so ADK records the tool as successful and a gateway
outage looks like a normal call. The tools' HTTP call goes through
`voice_assistant.gateway_client._send`, looked up in the module's globals on every call,
so wrapping that attribute gives each call:

- a CLIENT span `agent_gateway.request`, nested under ADK's execute_tool span, with the
  operation (the OpenAPI operationId), method, path template, HTTP status code and what the
  gateway answered: `agent_gateway.next_step`, or `agent_gateway.violation_code` and
  `agent_gateway.violation_action` when it refused. ERROR status (and the exception) on
  network errors, timeouts, 5xx and 401/403;
- (the ERROR log line is written by gateway_client.request itself, so each failure is
  logged once, and the tool's span is marked ERROR by observability/logs.py);
- `voice.gateway.duration` (Histogram, ms) by operation and outcome.

The caller's number (X-Caller-Id), the query parameters and the request body are never
recorded: only which operation ran and how it ended.
"""

import functools
import time

from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

_tracer = trace.get_tracer("maxis_voice_agent.observability")
_meter = metrics.get_meter("maxis_voice_agent.observability")
_latency = _meter.create_histogram(
    "voice.gateway.duration", unit="ms",
    description="Agent gateway latency for the roaming tools, by operation and outcome")

# (method, path) -> operationId in CustomerDocs/adk-handover-pack/05-tool-api-specs.
OPERATIONS = {
    ("GET", "/roaming/lines"): "listRoamingLines",
    ("GET", "/roaming/passes"): "findPasses",
    ("POST", "/roaming/purchases"): "buyPass",
    ("GET", "/roaming/purchases"): "listMyPasses",
    ("POST", "/roaming/wrong-pass"): "helpWithWrongPass",
    ("POST", "/roaming/switch"): "switchRoaming",
    ("POST", "/messages/whatsapp"): "sendWhatsAppLink",
}


def _server_address() -> str:
  from voice_assistant import gateway_client

  if gateway_client.is_mock():
    return "mock-gateway"
  return gateway_client.gateway_url().split("://", 1)[-1].split("/", 1)[0]


def _record_answer(span, resp) -> str:
  """Puts the gateway's answer on the span; returns the outcome for the metric."""
  code = resp.status_code
  span.set_attribute("http.response.status_code", code)
  if code >= 500 or code in (401, 403):
    span.set_status(Status(StatusCode.ERROR, f"HTTP {code}"))
    return f"http_{code}"
  try:
    data = resp.json()
  except ValueError:
    span.set_status(Status(StatusCode.ERROR, "unreadable response"))
    return "unreadable"
  violations = data.get("violations") or []
  if data.get("status") == "fail" and violations:
    first = violations[0] or {}
    span.set_attribute("agent_gateway.violation_code", str(first.get("code", ""))[:60])
    span.set_attribute("agent_gateway.violation_action", str(first.get("action", ""))[:60])
    # A refusal by Maxis's rules (not a Maxis number, line not found) is a valid answer, not
    # an error; only a backend failure reported by the gateway marks the span ERROR.
    if first.get("code") == "BACKEND_UNAVAILABLE":
      span.set_status(Status(StatusCode.ERROR, "BACKEND_UNAVAILABLE"))
    return f"fail_{first.get('code', 'unknown')}"
  step = (data.get("responseData") or {}).get("next_step")
  if step:
    span.set_attribute("agent_gateway.next_step", str(step)[:60])
  return "ok"


def wrap(send_fn):
  if getattr(send_fn, "_obs_wrapped", False) is True:
    return send_fn

  @functools.wraps(send_fn)
  async def traced_send(method, path, **kwargs):
    operation = OPERATIONS.get((method.upper(), path), "unknown")
    start = time.perf_counter()
    outcome = "ok"
    with _tracer.start_as_current_span("agent_gateway.request", kind=SpanKind.CLIENT, attributes={
        "server.address": _server_address(),
        "http.request.method": method.upper(),
        "url.template": path,
        "agent_gateway.operation": operation,
    }) as span:
      try:
        resp = await send_fn(method, path, **kwargs)
        outcome = _record_answer(span, resp)
        return resp
      except Exception as exc:
        outcome = type(exc).__name__
        raise  # start_as_current_span records the exception and sets ERROR
      finally:
        _latency.record((time.perf_counter() - start) * 1000, {"operation": operation, "outcome": outcome})

  traced_send._obs_wrapped = True
  return traced_send


def install() -> None:
  from voice_assistant import gateway_client

  gateway_client._send = wrap(gateway_client._send)
