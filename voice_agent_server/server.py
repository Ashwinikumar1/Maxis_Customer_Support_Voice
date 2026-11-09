"""Voice agent server (runs on Cloud Run). Telephony protocol only, no UI.

Every caller connects to  wss://HOST/ces/<location>  and speaks the CES BidiRunSession
protocol (see channels/ces_bridge.py):
  - production: Genesys -> ces-genesys-adapter -> /ces  (the adapter used by GECX)
  - testing   : ../client (browser UI + local proxy) -> /ces, the SAME protocol and
                audio format (MULAW 8 kHz) as Genesys, so tests cover the phone path.

GET /healthz -> 200 "ok" (no auth, no data). Everything else -> 404.
Transcripts are saved to GCS when each call ends.

Run:  uv run python server.py   ->  ws://127.0.0.1:8080/ces/local
"""

import asyncio
import os
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

from google.adk.sessions import InMemorySessionService  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402
from websockets.datastructures import Headers  # noqa: E402
from websockets.http11 import Response  # noqa: E402

from channels import ces_bridge  # noqa: E402
from streaming_core import (  # noqa: E402
    Call, CallInfo, default_run_config, logger)
from voice_assistant.agent import create_root_agent, warm_up_model  # noqa: E402
from voice_assistant.gateway.auth import warm_up_token  # noqa: E402
from voice_assistant.gateway.client import gateway_url  # noqa: E402
from opentelemetry import trace  # noqa: E402


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

# =============================================================================
# 2. Configuration (all overridable from .env)
# =============================================================================
APP_NAME = "voice_assistant"           # ADK app name; scopes sessions in the SessionService
HOST = os.getenv("HOST", "127.0.0.1")  # loopback for local dev; 0.0.0.0 only inside a container
PORT = int(os.getenv("PORT", "8080"))
VOICE_NAME = os.getenv("VOICE_NAME", "Kore")  # Gemini prebuilt voice
# BCP-47 speech language for the Live API (e.g. "en-US", "ms-MY"); empty = auto-detect.
LANGUAGE_CODE = os.getenv("LANGUAGE_CODE", "").strip() or None

# ADK SessionService stores conversation history/state per session.
# Created ONCE and shared by all connections (per ADK custom-server guidance).
# TODO: use a persistent SessionService (e.g. DatabaseSessionService) in production.
session_service = InMemorySessionService()


# =============================================================================
# 3. HTTP handshake: route + authenticate before any WebSocket opens
# =============================================================================
def _http(status: HTTPStatus, body: bytes) -> Response:
  headers = Headers([("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body))),
                     ("Connection", "close"), ("Cache-Control", "no-store"),
                     ("X-Content-Type-Options", "nosniff")])
  return Response(status.value, status.phrase, headers, body)


def process_request(connection, request):
  """Runs on EVERY incoming HTTP request, before any WebSocket handshake.

  - /ces/<location> -> Bearer token check, then None = continue the WebSocket handshake.
  - /healthz        -> 200 (Cloud Run / uptime checks).
  - anything else   -> 404.
  """
  path = request.path.split("?", 1)[0]
  if ces_bridge.is_bridge_path(path):
    if not ces_bridge.authorized(request.headers):
      logger.warning("CES bridge: rejected unauthenticated connection")
      return _http(HTTPStatus.UNAUTHORIZED, b"Unauthorized")
    return None
  if path == "/healthz":
    return _http(HTTPStatus.OK, b"ok")
  return _http(HTTPStatus.NOT_FOUND, b"Not found")


# =============================================================================
# 4. One WebSocket == one call
# =============================================================================
async def handle_client(websocket):
  # One root span per conversation: every ADK span (agent, model, tool) created
  # inside becomes a child, so Cloud Trace shows the whole call as one trace.
  # process_request only lets authenticated /ces paths get this far.
  with tracer.start_as_current_span("voice_session"):
    await ces_bridge.handle(websocket, start_call=_start_call)


_background: set[asyncio.Task] = set()  # keeps fire-and-forget tasks referenced


async def _start_call(info: CallInfo) -> Call:
  """Starts a call. CallInfo is all a channel must provide:
    MSISDN          -> ADK user_id, and X-Caller-Id on every gateway call
    conversation_id -> ADK session_id, and X-Conversation-Id on every gateway call
  """
  trace.get_current_span().set_attribute("session.id", info.conversation_id)  # trace <-> transcript
  call = await Call.start(info, session_service=session_service, app_name=APP_NAME,
                          agent_factory=create_root_agent,
                          run_config=default_run_config(VOICE_NAME, LANGUAGE_CODE))
  logger.info("Session %s started (%s, %s, msisdn ...%s)", info.conversation_id, info.channel,
              call.ctx["timezone"], info.msisdn[-4:])
  # Get the gateway token now, in the background, so the first roaming tool call
  # doesn't wait for it (cached for ~1 h; failures are ignored here).
  task = asyncio.create_task(asyncio.to_thread(warm_up_token, gateway_url()))
  _background.add(task)
  task.add_done_callback(_background.discard)
  return call


# =============================================================================
# 5. Entry point
# -----------------------------------------------------------------------------
# ping_interval=30 keeps idle connections alive through proxies/load balancers;
# max_size caps a single WebSocket message at 1 MB.
# =============================================================================
async def main():
  logger.info("Voice agent on ws://%s:%s/ces/<location> (project=%s, vertex=%s)", HOST, PORT,
              os.getenv("GOOGLE_CLOUD_PROJECT"), os.getenv("GOOGLE_GENAI_USE_VERTEXAI"))
  warm_up_model()  # Vertex credentials loaded now, not when the first caller is waiting
  async with serve(handle_client, HOST, PORT, process_request=process_request,
                   ping_interval=30, close_timeout=30, max_size=1 << 20) as server:
    await server.serve_forever()


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    logger.info("Exiting application...")
