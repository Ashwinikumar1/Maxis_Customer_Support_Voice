"""Security logging for the WebSocket endpoint (part of gap #9).

server.py rejects cross-site WebSocket attempts with a 403 but logs nothing.
It binds `serve` with `from websockets.asyncio.server import serve` when it is
imported, so replacing that module attribute before server.py runs lets us
wrap its `process_request` and log every 403 as a WARNING with the rejected
Origin, plus a `voice.ws.rejected` counter. Static-file traffic is not logged.
"""

import logging

from opentelemetry import metrics

logger = logging.getLogger("voice_agent.security")
_rejected = metrics.get_meter("maxis_voice_agent.observability").create_counter(
    "voice.ws.rejected", description="WebSocket upgrade requests rejected by the server, by HTTP status")


def _wrap_process_request(process_request):

  def logged(connection, request):
    response = process_request(connection, request)
    status = getattr(response, "status_code", None)
    if status == 403:
      origin = request.headers.get("Origin") or "<none>"
      logger.warning("WebSocket rejected: origin %s not allowed (path %s)", origin[:200], request.path[:200])
      _rejected.add(1, {"http.response.status_code": status})
    return response

  return logged


def install() -> None:
  import websockets.asyncio.server as ws_server

  original = ws_server.serve
  if getattr(original, "_obs_wrapped", False) is True:
    return

  def serve(handler, *args, **kwargs):
    if kwargs.get("process_request") is not None:
      kwargs["process_request"] = _wrap_process_request(kwargs["process_request"])
    return original(handler, *args, **kwargs)

  serve._obs_wrapped = True
  serve.__wrapped__ = original
  ws_server.serve = serve
