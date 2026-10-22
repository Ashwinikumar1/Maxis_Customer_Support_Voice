"""Bidi-streaming voice server.

One WebSocket per conversation at ws://HOST:PORT/ws; the browser UI is served
on the same port. Transcript is dumped to GCS when the session ends.

client -> server : {"type":"init","timezone"} {"type":"audio","data":b64 PCM16 16k}
                   {"type":"text","data":"..."} {"type":"ping"} {"type":"end"}
server -> client : {"type":"ready"} {"type":"audio","data":b64 PCM16 24k}
                   {"type":"transcript","role","text","partial","author"}
                   {"type":"tool_call","name","args","author"} {"type":"active_agent","name"}
                   {"type":"tool_result","name","response","author"}
                   {"type":"interrupted"} {"type":"turn_complete","state"} {"type":"error"}

Run:  uv run python server.py   ->  http://127.0.0.1:8080
"""

import asyncio
import base64
import binascii
import json
import mimetypes
import os
import re
import time
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
from streaming_core.utils import current_session_id, shutdown_requested  # noqa: E402
from voice_assistant.agent import create_root_agent  # noqa: E402
from opentelemetry import metrics, trace  # noqa: E402
from opentelemetry.trace import Link, SpanContext, TraceFlags  # noqa: E402
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator  # noqa: E402


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

# [Observability #13] Call-level metrics, recorded HERE - not derived from spans.
# WHY HERE: trace sampling can be switched on with OTEL_TRACES_SAMPLER (see
# .env.example). The OpenTelemetry SDK runs no span processor at all for a call
# that is not sampled (verified: Tracer.start_span returns a NonRecordingSpan),
# so metrics computed from spans would undercount by exactly the sampling rate,
# e.g. show 10% of real calls at OTEL_TRACES_SAMPLER_ARG=0.1. Metrics recorded
# directly are always complete, whatever the sampling setting.
#   voice.sessions.active  calls in progress on this instance (capacity planning:
#                          today's deploy settings allow 5 instances x 20 calls)
#   voice.session.duration call length in seconds, by outcome and close_reason
#   voice.sessions         finished calls, by outcome (ok / error) and close_reason
_meter = metrics.get_meter("maxis_voice_agent")
_sessions_active = _meter.create_up_down_counter(
    "voice.sessions.active", description="Voice calls in progress on this instance")
_session_duration = _meter.create_histogram(
    "voice.session.duration", unit="s", description="Voice call duration, by outcome and close_reason")
_sessions_finished = _meter.create_counter(
    "voice.sessions", description="Finished voice calls, by outcome and close_reason")

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
  name = _ROUTES.get(path)
  if not name:
    return _http(HTTPStatus.NOT_FOUND, b"Not found")
  ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
  return _http(HTTPStatus.OK, _FILES[name], ctype + ("; charset=utf-8" if "text" in ctype or "javascript" in ctype else ""))


# =============================================================================
# 4. Voice session: one call of handle_client == one conversation
# =============================================================================
def _request_trace_links(headers) -> list[Link]:
  """[Observability #6] Link this call's trace to Cloud Run's request trace.

  WHAT THIS DOES
    When the browser opens the WebSocket, Cloud Run (the platform in front of
    this server) keeps its OWN small record of that HTTP request, and sends us
    its tracking number in a request header. This function reads that number
    and returns it as a "span link" that is attached to the call's
    `voice_session` span in handle_client() below.

  WHY IT MATTERS IN THE DESIGN
    Two records describe the same call, and before this change nothing
    connected them:
      - Cloud Run's request record: shows problems AT THE FRONT DOOR -
        slow WebSocket handshakes, 5xx errors from the load balancer,
        connections rejected before our code even ran.
      - Our `voice_session` trace: shows everything INSIDE the call - the
        agent, each model turn, every FAQ search, errors.
    With the link, someone looking at a bad request in Cloud Run can jump to
    the full call trace (and back), instead of guessing by timestamps.

  WHY A *LINK* AND NOT A *PARENT* (important - do not "simplify" this)
    The obvious alternative is to make the call trace a CHILD of Cloud Run's
    request. That would be wrong here: Cloud Run records only a sample of
    requests - at most 0.1 requests per second per instance, i.e. about one
    request every 10 seconds - and marks all the others "not sampled".
    OpenTelemetry's default sampler obeys a parent's decision, so most of our
    call traces would silently disappear. A link is a "see also" reference:
    it connects the two traces without inheriting that sampling decision.
    Source: https://docs.cloud.google.com/run/docs/trace ("Trace sampling rate")

  WHICH HEADER
    Google recommends the standard W3C `traceparent` header and says Cloud Run
    fills it in automatically; the older `X-Cloud-Trace-Context` header is the
    documented fallback. Its format is TRACE_ID/SPAN_ID;o=1 and - unlike
    traceparent - the span id is DECIMAL, not hexadecimal, hence int(..., 10).
    Source: https://docs.cloud.google.com/trace/docs/trace-context

  Returns [] when neither header is present or valid (e.g. local runs), so the
  call trace is simply created without a link - nothing else changes.
  """
  traceparent = headers.get("traceparent")
  if traceparent:
    ctx = trace.get_current_span(
        TraceContextTextMapPropagator().extract({"traceparent": traceparent})).get_span_context()
    if ctx.is_valid:
      return [Link(ctx, {"link.source": "cloud_run_request"})]
  match = re.match(r"^([0-9a-fA-F]{32})/(\d+)(?:;o=(\d))?", headers.get("X-Cloud-Trace-Context") or "")
  if match:
    ctx = SpanContext(trace_id=int(match[1], 16), span_id=int(match[2]) & 0xFFFFFFFFFFFFFFFF, is_remote=True,
                      trace_flags=TraceFlags(TraceFlags.SAMPLED if match[3] == "1" else TraceFlags.DEFAULT))
    if ctx.is_valid:
      return [Link(ctx, {"link.source": "cloud_run_request"})]
  return []


async def handle_client(websocket):
  # One root span per conversation: every ADK span (agent, model, tool) created
  # inside becomes a child, so Cloud Trace shows the whole call as one trace.
  # [Observability #6] Attach the Cloud Run request trace as a link (see
  # _request_trace_links above for why it is a link and not a parent).
  request = getattr(websocket, "request", None)
  links = _request_trace_links(request.headers) if request is not None else []
  with tracer.start_as_current_span("voice_session", links=links):
    await _handle_client(websocket)


async def _handle_client(websocket):
  client_id = uuid.uuid4().hex  # random, unguessable id for this connection
  send = lambda payload: websocket.send(json.dumps(payload))  # noqa: E731
  await send({"type": "ready"})

  # --- 4a. Handshake: wait briefly for {"type":"init"} with the user's timezone.
  # The timezone drives the greeting and the date/time injected into the prompt.
  # If the client never sends it, fall back to DEFAULT_TIMEZONE after 5 s.
  timezone = None
  try:
    async with asyncio.timeout(INIT_TIMEOUT_S):
      while True:
        msg = _parse(await websocket.recv())
        if msg.get("type") == "init":
          timezone = str(msg.get("timezone") or "")[:64] or None
          break
  except TimeoutError:
    # [Observability #9c] Previously a silent `pass`. The timezone drives the
    # greeting ("Good morning") and the date given to the model, so a call that
    # fell back to DEFAULT_TIMEZONE can greet the caller wrongly. Logging it
    # explains such reports and shows how often clients fail to send `init`.
    logger.info("No init message within %.0f s - using the default timezone", INIT_TIMEOUT_S)
  ctx = get_datetime_info(timezone)  # {"timezone","date","time","greeting"}

  # --- 4b. Build the per-conversation ADK objects.
  #   session_service.create_session -> fresh ADK session (history + state=ctx)
  #   Runner                         -> executes the agent tree for this session
  #   create_root_agent(ctx)         -> new agent tree with today's date/greeting
  #   TranscriptLogger               -> collects turns, uploaded to GCS at the end
  #   LiveVoiceSession               -> the bidi loop between client and Gemini Live
  # TODO(security): no auth yet - derive user_id from a verified identity in prod.
  user_id, session_id = f"user_{client_id}", f"session_{client_id}"
  # [#13] Every log line from here on - including from tasks this call starts -
  # carries this session ID, whether or not the call's trace is sampled.
  current_session_id.set(session_id)
  # `call_span` is this call's `voice_session` span. It is kept in a variable
  # because the cleanup block at the end of this function records why the call
  # ended on it ([Observability #9c]).
  call_span = trace.get_current_span()
  call_span.set_attribute("session.id", session_id)  # match trace <-> transcript
  span_ctx = call_span.get_span_context()
  await session_service.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id, state=ctx)
  runner = Runner(app_name=APP_NAME, agent=create_root_agent(ctx), session_service=session_service)
  # [Observability #8a] Put the call's trace ID into the transcript file.
  # WHY: transcripts are what QA and complaint handling actually read. Before,
  # going from "this customer had a bad call" to that call's technical timeline
  # meant searching by time and guessing. Now the transcript's metadata.trace_id
  # can be pasted straight into Cloud Trace to see the turns, timings, FAQ
  # searches and errors of exactly that call. (session.id above already links
  # the other way: trace -> transcript.) None when tracing is off (local runs);
  # TranscriptLogger then leaves the field out.
  transcript = TranscriptLogger(session_id, channel="browser", timezone=ctx["timezone"],
                                trace_id=f"{span_ctx.trace_id:032x}" if span_ctx.is_valid else None)
  session = LiveVoiceSession(runner=runner, user_id=user_id, session_id=session_id,
                             run_config=default_run_config(VOICE_NAME), transcript=transcript)
  logger.info("Session %s started (%s)", session_id, ctx["timezone"])

  # --- 4c. UPSTREAM task: browser -> Gemini.
  # Reads WebSocket messages and pushes them into the session's LiveRequestQueue:
  # mic audio chunks (base64 PCM16 16 kHz) and typed text. Returns on "end" or
  # when the browser disconnects (the async-for loop simply finishes).
  #
  # [Observability #9c] Count client input that has to be thrown away.
  # WHAT: a message that is not valid JSON, or an audio chunk that is too large
  #   or not valid base64, cannot be used and is discarded (unchanged behaviour).
  # WHY IT MATTERS: before, these were discarded SILENTLY. A discarded audio
  #   chunk is a piece of the caller's speech that never reached Gemini, so the
  #   only symptom was a complaint like "the bot didn't hear me" with no
  #   evidence anywhere. Now each call keeps a count, which is written to the
  #   call's span and its closing log line, and the FIRST drop of each kind is
  #   logged as a WARNING.
  # WHY ONLY THE FIRST: a broken client can send dozens of bad chunks per
  #   second; one log line per chunk would flood Cloud Logging (and its bill).
  #   One warning plus a total keeps the signal without the noise.
  dropped = {"invalid_message": 0, "invalid_audio": 0}

  def _drop(kind: str, what: str):
    dropped[kind] += 1
    if dropped[kind] == 1:
      logger.warning("Session %s: dropped %s (further drops are counted)", session_id, what)

  async def upstream():
    async for raw in websocket:
      msg = _parse(raw)
      if not msg:
        _drop("invalid_message", "a malformed or non-JSON message")
        continue
      kind = msg.get("type")
      if kind == "audio":
        pcm = _b64(msg.get("data"))
        if pcm:
          session.send_audio(pcm)
        else:
          _drop("invalid_audio", "an oversized or invalid base64 audio frame")
      elif kind == "text":
        session.send_text(str(msg.get("data", "")))
      elif kind == "ping":
        await send({"type": "pong"})
      elif kind == "end":
        return "client_end"           # [#9c] the caller ended the call normally
    return "client_disconnect"        # [#9c] the socket closed without "end" (network drop, tab closed)

  # --- 4d. DOWNSTREAM callback: Gemini -> browser.
  # LiveVoiceSession calls emit(type, **data) for everything the model does.
  # Here we only translate to the browser's JSON protocol: audio bytes are
  # base64-encoded, every other event is forwarded as-is. A telephony channel
  # (e.g. Genesys) would provide its own emit() with audio format conversion.
  async def emit(kind, **data):
    if kind == "audio":
      await send({"type": "audio", "data": base64.b64encode(data["data"]).decode()})
    else:
      await send({"type": kind, **data})

  # --- 4e. Run upstream and downstream CONCURRENTLY.
  # Both directions must be live at the same time so the user can interrupt
  # (barge in) while the model is speaking. When either side finishes
  # (browser disconnects, "end", or a Live API error) we tear everything down.
  #
  # [Observability #9c] Record WHY every call ended.
  # The possible values, and what each one means for the caller:
  #   client_end          - the caller ended the call normally ("end" message)
  #   client_disconnect   - the connection dropped without "end" (network
  #                         problem, browser tab closed, phone lost signal)
  #   live_session_ended  - the Gemini Live session finished on its own
  #   error               - something failed in our code or the Live API
  #   server_shutdown     - this server instance was stopped (e.g. Cloud Run
  #                         sends SIGTERM during a deploy or scale-down)
  # WHY IT MATTERS: before, every call ended with the same "Session X closed"
  # line, so "how many calls drop unexpectedly?" - the most basic health
  # question for a voice line - could not be answered. Now the reason is put on
  # the voice_session span (attribute voice.close_reason), in the closing log
  # line, and (via the observability add-on) on the voice.sessions metric, so
  # it can be charted and alerted on. A rise in client_disconnect points to
  # network problems; a rise in server_shutdown means deploys are cutting
  # live calls.
  close_reason = "unknown"
  started = time.monotonic()
  _sessions_active.add(1)  # [#13] matched by add(-1) in the `finally` below
  tasks = [asyncio.create_task(upstream()), asyncio.create_task(session.run(emit))]
  try:
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in done:
      exc = task.exception()
      if exc and not isinstance(exc, ConnectionClosed):  # a normal disconnect is not an error
        close_reason = "error"
        raise exc
      if exc:
        close_reason = "client_disconnect"
      elif task is tasks[0]:
        close_reason = task.result()
      else:
        close_reason = "live_session_ended"
    # [#9c] During a shutdown the server closes every socket itself, which the
    # lines above cannot tell apart from the caller dropping. The shutdown flag
    # (see streaming_core/utils.py) makes the recorded reason truthful.
    if shutdown_requested.is_set() and close_reason in ("client_disconnect", "live_session_ended"):
      close_reason = "server_shutdown"
  except asyncio.CancelledError:
    # [#9c] This task was cancelled from outside - in practice the server is
    # shutting down (Cloud Run sends SIGTERM and allows ~10 s before killing
    # the container). Record it, then re-raise: cancellation must propagate so
    # the shutdown can finish; the `finally` below still saves the transcript.
    close_reason = "server_shutdown"
    raise
  except Exception:
    # Log full details server-side; send only a generic message to the client.
    logger.exception("Session %s failed", session_id)
    try:
      await send({"type": "error", "message": "The voice session ended unexpectedly."})
    except ConnectionClosed:
      pass
  finally:
    # --- 4f. Cleanup (always runs): stop both tasks, close the Live connection,
    # upload the transcript ONCE to GCS, then close the browser socket.
    for task in tasks:
      task.cancel()
    session.close()
    # [#13] Call metrics FIRST, before anything below that awaits: if the
    # cleanup were interrupted (e.g. a second cancellation during shutdown),
    # voice.sessions.active must still go back down, or the dashboard would
    # show calls that no longer exist. Complete even when this call's trace is
    # not sampled. outcome: "error" only when the call failed in our code or
    # the Live API; every other ending (hang-up, network drop, shutdown) is
    # "ok" - the close_reason attribute tells those apart.
    duration_s = time.monotonic() - started
    outcome = "error" if close_reason == "error" else "ok"
    call_metric_attrs = {"outcome": outcome, "close_reason": close_reason}
    _sessions_active.add(-1)
    _session_duration.record(duration_s, call_metric_attrs)
    _sessions_finished.add(1, call_metric_attrs)
    # [Observability #12] Token usage of this call, taken ONCE here and used
    # everywhere: the transcript (saved next, so it must be set first), the
    # call's span and the closing log line below, and the voice.call.tokens
    # metric (recorded inside record_token_usage).
    # WHY: Google Cloud only reports tokens as project-wide totals per model,
    # and ADK records no token metric for live voice calls, so without this
    # there is no way to tell what an individual call consumed - e.g. which
    # calls are unusually expensive, or what an average call costs.
    # {} when the Live API sent no usage report (e.g. the call failed first).
    tokens = session.record_token_usage()
    transcript.set_metadata("token_usage", tokens or None)
    await transcript.save()  # dump once per session
    await websocket.close()
    # [#9c] One summary per call: reason, length, and how much input was
    # discarded. On the span it can be filtered in Trace Explorer; in the log
    # line it is readable at a glance. Example:
    #   Session session_ab12 closed (reason=client_end, duration=184.2s,
    #   invalid_audio=0, invalid_messages=0)
    call_span.set_attributes({
        "voice.close_reason": close_reason,
        "voice.dropped.invalid_audio": dropped["invalid_audio"],
        "voice.dropped.invalid_message": dropped["invalid_message"],
        # [#12] e.g. voice.tokens.input, voice.tokens.output, voice.tokens.total,
        # voice.tokens.input.AUDIO - filterable per call in Trace Explorer.
        **{f"voice.tokens.{key}": value for key, value in tokens.items()},
    })
    # [#12] Tokens are in the same closing line; "n/a" means no usage report arrived.
    logger.info("Session %s closed (reason=%s, duration=%.1fs, invalid_audio=%d, invalid_messages=%d, "
                "tokens_in=%s, tokens_out=%s, tokens_total=%s)",
                session_id, close_reason, duration_s, dropped["invalid_audio"], dropped["invalid_message"],
                tokens.get("input", "n/a"), tokens.get("output", "n/a"), tokens.get("total", "n/a"))


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
