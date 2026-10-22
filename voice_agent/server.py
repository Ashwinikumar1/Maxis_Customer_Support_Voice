"""Bidi-streaming voice server.

One WebSocket per conversation at ws://HOST:PORT/ws; the browser UI is served
on the same port. Transcript is dumped to GCS when the session ends.

client -> server : {"type":"init","timezone","caller"} {"type":"audio","data":b64 PCM16 16k}
                   {"type":"text","data":"..."} {"type":"ping"} {"type":"end"}
                   {"type":"mock","action":"fail_next","operation","count"}   (mock gateway only)
server -> client : {"type":"ready","routing_mode","gateway"} {"type":"audio","data":b64 PCM16 24k}
                   {"type":"transcript","role","text","partial","author"}
                   {"type":"tool_call","name","args","author"} {"type":"active_agent","name"}
                   {"type":"tool_result","name","response","author"}
                   {"type":"interrupted"} {"type":"turn_complete","state"} {"type":"error"}
                   {"type":"handover","session_escalated","reason","params","agent"}
                   {"type":"call_ended"}

"caller" picks a test caller (GET /api/test-callers) whose number becomes the
session's caller_id, as Genesys' calling number will. The browser never sends a
phone number itself. When an agent calls end_session, the server sends
"handover", lets the goodbye finish, then sends "call_ended" and closes.

Run:  uv run python server.py   ->  http://127.0.0.1:8080
"""

import asyncio
import base64
import binascii
import json
import mimetypes
import os
import uuid
from http import HTTPStatus
from pathlib import Path

import certifi
from dotenv import load_dotenv

# =============================================================================
# 1. Environment setup
# -----------------------------------------------------------------------------
# Load .env BEFORE importing ADK / google-genai: they read GOOGLE_GENAI_USE_VERTEXAI,
# GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION at import/client-creation time.
# SSL_CERT_FILE: macOS Python often lacks system CAs, which breaks the Live
# API's secure WebSocket (CERTIFICATE_VERIFY_FAILED) - certifi's bundle fixes it.
# =============================================================================
load_dotenv(Path(__file__).parent / ".env")
os.environ.setdefault("SSL_CERT_FILE", certifi.where())  # macOS CA bundle for Live API

from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402
from websockets.datastructures import Headers  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402
from websockets.http11 import Response  # noqa: E402

from streaming_core import (  # noqa: E402
    LiveVoiceSession, TranscriptLogger, default_run_config, get_datetime_info, logger)
from voice_assistant import gateway_client  # noqa: E402
from voice_assistant.agent import create_root_agent, routing_mode  # noqa: E402
from opentelemetry import metrics, trace  # noqa: E402


def _setup_otel() -> None:
  """OpenTelemetry -> Google Cloud (traces, metrics, logs) when OTEL_TO_CLOUD=true.

  - Traces  -> Cloud Trace: one `voice_session` trace per call with ADK's
    invoke_agent / execute_tool spans and our `model_turn` spans.
  - Metrics -> Cloud Monitoring: ADK GenAI metrics + voice.time_to_first_audio,
    voice.model_turns.
  - Logs    -> Cloud Logging: ADK's GenAI OTel log events, correlated to traces.
  All three go over OTLP to telemetry.googleapis.com via ADK's own exporters.
  Service name comes from OTEL_SERVICE_NAME. Caller speech stays out of spans
  unless ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=true (PII).
  """
  if os.getenv("OTEL_TO_CLOUD", "false").lower() != "true":
    return
  # Cloud Monitoring rejects metrics without a region; Cloud Run's detected
  # region overrides this default, which only matters for local runs.
  os.environ.setdefault(
      "OTEL_RESOURCE_ATTRIBUTES", f"cloud.region={os.getenv('GOOGLE_CLOUD_LOCATION', 'us-central1')}")
  from google.adk.telemetry.google_cloud import get_gcp_exporters, get_gcp_resource
  from google.adk.telemetry.setup import maybe_set_otel_providers
  maybe_set_otel_providers(
      [get_gcp_exporters(enable_cloud_tracing=True, enable_cloud_metrics=True,
                         enable_cloud_logging=True)],
      otel_resource=get_gcp_resource(os.getenv("GOOGLE_CLOUD_PROJECT")))
  logger.info("OpenTelemetry enabled: traces, metrics, logs -> Google Cloud (%s)",
              os.getenv("OTEL_SERVICE_NAME", "unknown_service"))


_setup_otel()
tracer = trace.get_tracer("maxis_voice_agent")
# Call outcomes for the design's dashboards (section 8): containment rate =
# contained / all calls; handoff rate = escalated / all calls.
_meter = metrics.get_meter("maxis_voice_agent")
_calls = _meter.create_counter(
    "voice.calls", description="Finished calls by outcome (contained, escalated, abandoned)")

# =============================================================================
# 2. Configuration (all overridable from .env)
# =============================================================================
APP_NAME = "voice_assistant"           # ADK app name; scopes sessions in the SessionService
HOST = os.getenv("HOST", "127.0.0.1")  # loopback for local dev; 0.0.0.0 only inside a container
PORT = int(os.getenv("PORT", "8080"))
VOICE_NAME = os.getenv("VOICE_NAME", "Kore")  # Gemini prebuilt voice
# Browser pages allowed to open the WebSocket (defence against cross-site WS hijacking).
ALLOWED_ORIGINS = {
    o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS", f"http://127.0.0.1:{PORT},http://localhost:{PORT}").split(",") if o.strip()
}
INIT_TIMEOUT_S = 5.0        # how long to wait for the client's {"type":"init"} message
MAX_B64_LEN = 512 * 1024    # reject oversized audio frames (DoS protection)
END_GRACE_S = 1.5           # after end_session: let the last audio reach the client
END_MAX_WAIT_S = 15.0       # ... but never keep the call open longer than this

# Test callers for the browser client: the picker's choice becomes the session's
# caller_id (Genesys will pass the real calling number instead). Turn off when
# the browser client is exposed beyond the delivery team.
ALLOW_TEST_CALLERS = os.getenv("ALLOW_TEST_CALLERS", "true").lower() == "true"
MOCK_RESET_ON_CALL = os.getenv("MOCK_RESET_ON_CALL", "true").lower() == "true"
if ALLOW_TEST_CALLERS:
  from mock_gateway import TEST_CALLERS
  _CALLERS = {c["key"]: c for c in TEST_CALLERS}
else:
  _CALLERS = {}

# ADK SessionService stores conversation history/state per session.
# Created ONCE and shared by all connections (per ADK custom-server guidance).
# TODO: use a persistent SessionService (e.g. DatabaseSessionService) in production.
session_service = InMemorySessionService()

# =============================================================================
# 3. Static UI serving (plain HTTP on the same port as the WebSocket)
# -----------------------------------------------------------------------------
# Files are read into memory at startup and served from a FIXED allow-list
# (_ROUTES), so a request path can never reach the filesystem (no path traversal).
# Every response carries strict security headers (CSP, no framing, mic only).
# =============================================================================
STATIC_DIR = Path(__file__).parent / "static"
_ROUTES = {"/": "index.html", **{f"/static/{p.name}": p.name for p in STATIC_DIR.iterdir() if p.is_file()}}
_FILES = {name: (STATIC_DIR / name).read_bytes() for name in _ROUTES.values()}
_SECURITY_HEADERS = [
    ("Content-Security-Policy",
     "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:;"
     " connect-src 'self'; frame-src 'none';"
     " object-src 'none'; base-uri 'none'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "microphone=(self), camera=(), geolocation=()"),
    ("Cache-Control", "no-store"),
]


def _http(status: HTTPStatus, body: bytes, ctype: str = "text/plain; charset=utf-8") -> Response:
  """Builds a plain HTTP response (used for the UI files and for 403/404)."""
  headers = Headers([("Content-Type", ctype), ("Content-Length", str(len(body))),
                     ("Connection", "close"), *_SECURITY_HEADERS])
  return Response(status.value, status.phrase, headers, body)


def process_request(connection, request):
  """Runs on EVERY incoming HTTP request, before any WebSocket handshake.

  - "/ws"  -> check the Origin header, then return None so the WebSocket
              handshake continues and `handle_client` is called.
  - other  -> answer as a normal web server (UI files) or 404.
  Note: websockets logs these HTTP answers as "connection rejected (200 OK)";
  that is expected and just means a file was served.
  """
  path = request.path.split("?", 1)[0]
  if path == "/ws":
    if request.headers.get("Origin") not in ALLOWED_ORIGINS:  # cross-site WS hijacking
      return _http(HTTPStatus.FORBIDDEN, b"Forbidden")
    return None  # continue WebSocket handshake
  if path == "/api/test-callers":
    # Labels and hints only; the numbers stay on the server.
    callers = [{"key": c["key"], "label": c["label"], "hint": c["hint"]} for c in _CALLERS.values()]
    body = json.dumps({"callers": callers, "routing_mode": routing_mode(),
                       "gateway": "mock" if gateway_client.is_mock() else "remote"}).encode()
    return _http(HTTPStatus.OK, body, "application/json")
  name = _ROUTES.get(path)
  if not name:
    return _http(HTTPStatus.NOT_FOUND, b"Not found")
  ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
  return _http(HTTPStatus.OK, _FILES[name], ctype + ("; charset=utf-8" if "text" in ctype or "javascript" in ctype else ""))


# =============================================================================
# 4. Voice session: one call of handle_client == one conversation
# =============================================================================
async def handle_client(websocket):
  # One root span per conversation: every ADK span (agent, model, tool) created
  # inside becomes a child, so Cloud Trace shows the whole call as one trace.
  with tracer.start_as_current_span("voice_session"):
    await _handle_client(websocket)


async def _handle_client(websocket):
  client_id = uuid.uuid4().hex  # random, unguessable id for this connection
  send = lambda payload: websocket.send(json.dumps(payload))  # noqa: E731
  await send({"type": "ready", "routing_mode": routing_mode(),
              "gateway": "mock" if gateway_client.is_mock() else "remote"})

  # --- 4a. Handshake: wait briefly for {"type":"init"} with the user's timezone
  # and the chosen test caller. The timezone drives the greeting; dates for
  # roaming are always Malaysia time. If the client never sends init, fall back
  # to DEFAULT_TIMEZONE and a withheld number after 5 s.
  timezone = None
  caller_key = ""
  try:
    async with asyncio.timeout(INIT_TIMEOUT_S):
      while True:
        msg = _parse(await websocket.recv())
        if msg.get("type") == "init":
          timezone = str(msg.get("timezone") or "")[:64] or None
          caller_key = str(msg.get("caller") or "")[:32]
          break
  except TimeoutError:
    pass
  ctx = get_datetime_info(timezone)  # {"timezone","date","time","greeting"}

  # caller_id: the calling number, as Genesys will pass it. In the browser it
  # comes from the server-side test-caller table, never from the client.
  caller = _CALLERS.get(caller_key)
  caller_id = caller["msisdn"] if caller else ""
  if caller_id and gateway_client.is_mock() and MOCK_RESET_ON_CALL:
    from mock_gateway import gateway
    gateway().reset(caller_id)  # every demo call starts from the same account state

  # --- 4b. Build the per-conversation ADK objects.
  #   session_service.create_session -> fresh ADK session (history + state=ctx + caller_id)
  #   Runner                         -> executes the agent tree for this session
  #   create_root_agent(ctx)         -> new agent tree with today's date/greeting
  #   TranscriptLogger               -> collects turns, uploaded to GCS at the end
  #   LiveVoiceSession               -> the bidi loop between client and Gemini Live
  # TODO(security): no auth yet - derive user_id from a verified identity in prod.
  user_id, session_id = f"user_{client_id}", f"session_{client_id}"
  span = trace.get_current_span()
  span.set_attribute("session.id", session_id)  # match trace <-> transcript
  span.set_attribute("voice.test_caller", caller_key or "withheld")
  await session_service.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id,
                                       state={**ctx, "caller_id": caller_id})
  runner = Runner(app_name=APP_NAME, agent=create_root_agent(ctx), session_service=session_service)
  transcript = TranscriptLogger(session_id, channel="browser", timezone=ctx["timezone"],
                                routing_mode=routing_mode(), test_caller=caller_key or "withheld",
                                caller_id=_mask(caller_id),
                                gateway="mock" if gateway_client.is_mock() else "remote")
  session = LiveVoiceSession(runner=runner, user_id=user_id, session_id=session_id,
                             run_config=default_run_config(VOICE_NAME), transcript=transcript)
  logger.info("Session %s started (%s, caller %s)", session_id, ctx["timezone"], caller_key or "withheld")

  ended = asyncio.Event()       # set when the bot's part of the call is over
  outcome: dict = {}            # end_session result, once called
  last_agent = {"name": ""}

  # --- 4c. UPSTREAM task: browser -> Gemini.
  # Reads WebSocket messages and pushes them into the session's LiveRequestQueue:
  # mic audio chunks (base64 PCM16 16 kHz) and typed text. Returns on "end" or
  # when the browser disconnects (the async-for loop simply finishes).
  async def upstream():
    async for raw in websocket:
      msg = _parse(raw)
      kind = msg.get("type")
      if kind == "audio":
        session.send_audio(_b64(msg.get("data")))
      elif kind == "text":
        session.send_text(str(msg.get("data", "")))
      elif kind == "ping":
        await send({"type": "pong"})
      elif kind == "mock":
        _mock_control(msg)
      elif kind == "end":
        return

  # --- 4d. DOWNSTREAM callback: Gemini -> browser.
  # LiveVoiceSession calls emit(type, **data) for everything the model does.
  # Here we only translate to the browser's JSON protocol: audio bytes are
  # base64-encoded, every other event is forwarded as-is. A telephony channel
  # (e.g. Genesys) would provide its own emit() with audio format conversion.
  # end_session: forward the handover, then end once the goodbye turn is done.
  loop = asyncio.get_running_loop()

  async def emit(kind, **data):
    if kind == "audio":
      await send({"type": "audio", "data": base64.b64encode(data["data"]).decode()})
      return
    await send({"type": kind, **data})
    if kind == "active_agent":
      last_agent["name"] = data.get("name", "")
    elif kind == "tool_call" and data.get("name") == "end_session":
      last_agent["end_args"] = dict(data.get("args") or {})
    elif kind == "tool_result" and data.get("name") == "end_session" and not outcome:
      # The tool's cleaned result is in session state; fall back to the call's args.
      stored = await session_service.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
      saved = stored.state.get("end_session") if stored else None
      if not saved:
        args = last_agent.get("end_args", {})
        saved = {"session_escalated": bool(args.get("session_escalated")), "reason": args.get("reason", ""),
                 "agent": data.get("author", ""),
                 "params": {k: v for k, v in args.items() if k not in ("session_escalated", "reason")}}
      outcome.update(saved)
      transcript.add_event("end_session", **outcome)
      await send({"type": "handover", **outcome})
      loop.call_later(END_MAX_WAIT_S, ended.set)
    elif kind == "turn_complete" and outcome and data.get("state") != "IN_PROGRESS":
      loop.call_later(END_GRACE_S, ended.set)

  async def wait_for_end():
    await ended.wait()
    await send({"type": "call_ended"})

  # --- 4e. Run upstream and downstream CONCURRENTLY.
  # Both directions must be live at the same time so the user can interrupt
  # (barge in) while the model is speaking. When either side finishes
  # (browser disconnects, "end", end_session, or a Live API error) we tear everything down.
  tasks = [asyncio.create_task(upstream()), asyncio.create_task(session.run(emit)),
           asyncio.create_task(wait_for_end())]
  try:
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in done:
      exc = task.exception()
      if exc and not isinstance(exc, ConnectionClosed):  # a normal disconnect is not an error
        raise exc
  except Exception:
    # Log full details server-side; send only a generic message to the client.
    logger.exception("Session %s failed", session_id)
    try:
      await send({"type": "error", "message": "The voice session ended unexpectedly."})
    except ConnectionClosed:
      pass
  finally:
    # --- 4f. Cleanup (always runs): stop the tasks, close the Live connection,
    # record the call outcome, upload the transcript ONCE, then close the socket.
    for task in tasks:
      task.cancel()
    session.close()
    result = ("escalated" if outcome.get("session_escalated") else "contained") if outcome else "abandoned"
    params = outcome.get("params") or {}
    _calls.add(1, {"outcome": result, "intent": params.get("intent", "none"),
                   "routing_mode": routing_mode(), "last_agent": outcome.get("agent") or last_agent["name"]})
    transcript.data["metadata"]["outcome"] = result
    await transcript.save()  # dump once per session
    await websocket.close()
    logger.info("Session %s closed (%s)", session_id, result)


def _mask(msisdn: str) -> str:
  """Keeps only the last 4 digits of a number for logs and transcripts."""
  return ("*" * max(0, len(msisdn) - 4) + msisdn[-4:]) if msisdn else ""


def _mock_control(msg: dict):
  """Demo controls from the browser, honoured only with the in-process mock gateway."""
  if not (ALLOW_TEST_CALLERS and gateway_client.is_mock()):
    return
  if msg.get("action") == "fail_next":
    from mock_gateway import gateway
    count = msg.get("count", 2)
    gateway().fail_next(str(msg.get("operation") or "*")[:40], count if isinstance(count, int) else 2)
    logger.info("Mock gateway: next %s call(s) will fail", count)


# =============================================================================
# 5. Input validation helpers (all client data is untrusted)
# =============================================================================
def _parse(raw) -> dict:
  """JSON text frame -> dict. Anything malformed becomes {} and is ignored."""
  if not isinstance(raw, str):
    return {}
  try:
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}
  except json.JSONDecodeError:
    return {}


def _b64(raw) -> bytes:
  """Base64 audio -> bytes, with a size cap and strict decoding. Invalid -> b""."""
  if not isinstance(raw, str) or len(raw) > MAX_B64_LEN:
    return b""
  try:
    return base64.b64decode(raw, validate=True)
  except (binascii.Error, ValueError):
    return b""


# =============================================================================
# 6. Entry point: one server handles both HTTP (UI) and WebSocket (/ws)
# -----------------------------------------------------------------------------
# ping_interval=30 keeps idle connections alive through proxies/load balancers;
# max_size caps a single WebSocket message at 1 MB.
# =============================================================================
async def main():
  logger.info("Voice agent on http://%s:%s (project=%s, vertex=%s)", HOST, PORT,
              os.getenv("GOOGLE_CLOUD_PROJECT"), os.getenv("GOOGLE_GENAI_USE_VERTEXAI"))
  async with serve(handle_client, HOST, PORT, process_request=process_request,
                   ping_interval=30, close_timeout=30, max_size=1 << 20) as server:
    await server.serve_forever()


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    logger.info("Exiting application...")
