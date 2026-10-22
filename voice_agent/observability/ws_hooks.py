"""Security logging for the WebSocket endpoint (part of gap #9).

server.py rejects cross-site WebSocket attempts with a 403 but logs nothing.
It binds `serve` with `from websockets.asyncio.server import serve` when it is
imported, so replacing that module attribute before server.py runs lets us
wrap its `process_request` and log every 403 as a WARNING with the rejected
Origin, plus a `voice.ws.rejected` counter. Static-file traffic is not logged.

It also remembers each websockets Server it creates, so shutdown.py can close
open calls cleanly on SIGTERM (see close_servers below).
"""

import logging
import weakref

from opentelemetry import metrics

logger = logging.getLogger("voice_agent.security")
_rejected = metrics.get_meter("maxis_voice_agent.observability").create_counter(
    "voice.ws.rejected", description="WebSocket upgrade requests rejected by the server, by HTTP status")
_servers = weakref.WeakSet()  # websockets Server objects created through serve()


def close_servers() -> None:
  """Asks every running WebSocket server to close its open calls.

  WHY: on Python 3.12 (the version in the container image) asyncio does NOT
  close open connections when the server is stopped - it waits for the callers
  to leave (Server.close_clients() only exists from 3.13). With a call in
  progress, shutdown then took ~10.6 s, past Cloud Run's 10 s limit, and the
  container was killed. websockets' Server.close() closes every open WebSocket
  with code 1001 ("going away") and waits for each call's cleanup (transcript
  upload, span end) - verified in websockets 15.0.1 (asyncio/server.py).

  Safe to call from a signal handler: close() is scheduled on the event loop
  with call_soon_threadsafe(), so it runs as ordinary loop code.
  """
  for server in list(_servers):
    try:
      server.get_loop().call_soon_threadsafe(server.close)
    except Exception:
      pass


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
    started = original(handler, *args, **kwargs)
    if getattr(started, "server", None) is not None:  # websockets keeps its Server here
      _servers.add(started.server)
    return started

  serve._obs_wrapped = True
  serve.__wrapped__ = original
  ws_server.serve = serve
