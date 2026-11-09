"""Local test client for the voice agent: browser UI + proxy to EITHER:
  1. The Genesys Adapter (wss://ces-genesys-adapter-adk-stg-.../  - Genesys Audio Connector v2 protocol)
  2. The Voice Agent Server directly (wss://.../ces/<location>    - CES BidiRunSession protocol)

When SERVER_URL does NOT contain "/ces/", client.py speaks the Genesys Audio Connector v2
protocol (X-API-KEY + HMAC Signature, `open` -> `opened`, binary 8 kHz PCMU frames, `disconnect` -> `close`),
testing the FULL Cloud Run chain:
  Browser <-> client.py (Genesys protocol) <-> Cloud Run Adapter <-> Cloud Run Voice Agent <-> Gateway

Run:  uv run python client.py   ->  http://127.0.0.1:8090
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import subprocess
import time
import uuid
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, InvalidURI
from websockets.http11 import Response

import ces
from audio import pcm16_to_ulaw, resample_pcm16, ulaw_to_pcm16

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

# --- Settings (voice_agent_client/.env) -----------------------------------------------------
SERVER_URL = os.getenv("SERVER_URL", "ws://127.0.0.1:8080/ces/local")
IS_GENESYS_ADAPTER = "/ces/" not in urlparse(SERVER_URL).path
GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "mxs-maxisapp-prd")
GENESYS_API_KEY_SECRET = os.getenv("GENESYS_API_KEY_SECRET", "genesys-audioconnector-api-key-stg")
GENESYS_CLIENT_SECRET_NAME = os.getenv("GENESYS_CLIENT_SECRET_NAME", "genesys-audioconnector-client-secret-stg")

HOST = os.getenv("CLIENT_HOST", "127.0.0.1")
PORT = int(os.getenv("CLIENT_PORT", "8090"))
TOKEN = ces.load_token()
MIN_TOKEN_LEN = 32
# Only this local page may open /ws (defence against cross-site WebSocket hijacking).
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
MIC_RATE, SPEAKER_RATE = 16000, 24000   # browser AudioContexts (static/app.js)
INIT_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 20.0
MAX_B64_LEN = 512 * 1024
MAX_TEXT_LEN = 2000
MAX_GOODBYE_WAIT_S = 20.0               # after endSession: let queued bot audio finish
DEBUG_KINDS = {"transcript", "tool_call", "tool_result", "active_agent", "turn_complete",
               "call_outcome"}
CLOUD_RUN_IAM = os.getenv("CLOUD_RUN_IAM", "auto").strip().lower()
_ID_TOKEN = {"value": "", "at": 0.0}
_GENESYS_CREDS = {"api_key": os.getenv("GENESYS_API_KEY", "").strip(),
                  "client_secret": os.getenv("GENESYS_CLIENT_SECRET", "").strip()}


def _get_id_token() -> str:
  if not _ID_TOKEN["value"] or time.time() - _ID_TOKEN["at"] > 50 * 60:
    out = subprocess.run(["gcloud", "auth", "print-identity-token"], capture_output=True, text=True, timeout=60)
    if out.returncode != 0 or not out.stdout.strip():
      raise RuntimeError("gcloud auth print-identity-token failed: run `gcloud auth login`")
    _ID_TOKEN.update(value=out.stdout.strip(), at=time.time())
  return _ID_TOKEN["value"]


def _ensure_genesys_creds() -> tuple[str, bytes]:
  if not _GENESYS_CREDS["api_key"] or not _GENESYS_CREDS["client_secret"]:
    for key, secret_name in (("api_key", GENESYS_API_KEY_SECRET), ("client_secret", GENESYS_CLIENT_SECRET_NAME)):
      if not _GENESYS_CREDS[key]:
        out = subprocess.run(
            ["gcloud", "secrets", "versions", "access", "latest",
             f"--secret={secret_name}", f"--project={GCP_PROJECT}"],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0 or not out.stdout.strip():
          raise RuntimeError(f"Failed to read secret {secret_name} from {GCP_PROJECT}")
        _GENESYS_CREDS[key] = out.stdout.strip()
  return _GENESYS_CREDS["api_key"], base64.b64decode(_GENESYS_CREDS["client_secret"])


def _genesys_signed_headers(url: str) -> dict:
  api_key, secret = _ensure_genesys_creds()
  u = urlparse(url)
  comps = '"@request-target" "@authority" "x-api-key"'
  params = f'created={int(time.time())};keyid="test";nonce="{uuid.uuid4().hex}";alg="hmac-sha256"'
  base = "\n".join([
      f'"@request-target": {u.path or "/"}',
      f'"@authority": {u.netloc}',
      f'"x-api-key": {api_key}',
      f'"@signature-params": ({comps});{params}',
  ])
  sig = base64.b64encode(hmac.new(secret, base.encode(), hashlib.sha256).digest()).decode()
  headers = {
      "X-API-KEY": api_key,
      "Signature-Input": f"sig1=({comps});{params}",
      "Signature": f"sig1=:{sig}:",
  }
  host = u.hostname or ""
  if CLOUD_RUN_IAM != "false" and (CLOUD_RUN_IAM != "auto" or host.endswith(".run.app")):
    headers["Authorization"] = f"Bearer {_get_id_token()}"
  return headers


def _cloud_run_headers() -> dict:
  host = urlparse(SERVER_URL).hostname or ""
  if CLOUD_RUN_IAM == "false" or (CLOUD_RUN_IAM == "auto" and not host.endswith(".run.app")):
    return {}
  return {"X-Serverless-Authorization": f"Bearer {_get_id_token()}"}


# --- Static UI: fixed allow-list read at startup (no path traversal) -------------
STATIC_DIR = ROOT / "static"
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
  headers = Headers([("Content-Type", ctype), ("Content-Length", str(len(body))),
                     ("Connection", "close"), *_SECURITY_HEADERS])
  return Response(status.value, status.phrase, headers, body)


def process_request(connection, request):
  """UI files over plain HTTP; /ws (same-origin only) continues to the WebSocket handshake."""
  path = request.path.split("?", 1)[0]
  if path == "/ws":
    if request.headers.get("Origin") not in ALLOWED_ORIGINS:
      return _http(HTTPStatus.FORBIDDEN, b"Forbidden")
    return None
  name = _ROUTES.get(path)
  if not name:
    return _http(HTTPStatus.NOT_FOUND, b"Not found")
  ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
  return _http(HTTPStatus.OK, _FILES[name], ctype + ("; charset=utf-8" if "text" in ctype or "javascript" in ctype else ""))


# --- One browser session == one "phone call" -------------------------------------
async def handle_browser(browser):
  if IS_GENESYS_ADAPTER:
    await _handle_browser_via_adapter(browser)
  else:
    await _handle_browser_direct_ces(browser)


async def _handle_browser_via_adapter(browser):
  """Browser <-> Genesys Audio Connector v2 protocol <-> ces-genesys-adapter-adk-stg <-> maxis-voice-agent."""
  send = lambda payload: browser.send(json.dumps(payload))  # noqa: E731

  init = await _wait_for_init(browser)
  if init is None:
    return
  msisdn = ces.normalise_msisdn(init.get("msisdn"))
  if not msisdn:
    await send({"type": "error", "message": "Enter a valid caller number (8-15 digits)."})
    await browser.close()
    return
  timezone = str(init.get("timezone") or "")[:64]
  conversation_id = str(uuid.uuid4())
  session_id = str(uuid.uuid4())
  seq = {"client": 0, "server": 0}

  server = await _connect_server(send)
  if server is None:
    await browser.close()
    return
  print(f"call {conversation_id} (via Genesys Adapter): caller ...{msisdn[-4:]} -> {SERVER_URL}", flush=True)

  async def send_genesys(kind: str, parameters: dict | None = None):
    seq["client"] += 1
    await server.send(json.dumps({
        "version": "2",
        "type": kind,
        "seq": seq["client"],
        "serverseq": seq["server"],
        "id": session_id,
        "position": "PT0S",
        "parameters": parameters or {},
    }))

  input_vars = {
      "_agent_id": f"projects/{GCP_PROJECT}/locations/us/apps/adk",
      "caller_id": f"+{msisdn}",
  }
  if timezone:
    input_vars["timezone"] = timezone

  try:
    await send_genesys("open", {
        "organizationId": str(uuid.uuid4()),
        "conversationId": conversation_id,
        "participant": {"id": str(uuid.uuid4()), "ani": f"+{msisdn}", "aniName": "", "dnis": "+60312345678"},
        "media": [{"type": "audio", "format": "PCMU", "channels": ["external"], "rate": ces.RATE}],
        "language": "en-US",
        "inputVariables": input_vars,
    })
    raw_first = await asyncio.wait_for(server.recv(), timeout=15.0)
    first = _parse(raw_first)
    seq["server"] = first.get("seq", seq["server"])
    if first.get("type") != "opened":
      info = (first.get("parameters") or {}).get("info") or f"unexpected {first.get('type')}"
      print(f"call {conversation_id}: adapter rejected open: {info}", flush=True)
      await send({"type": "error", "message": f"Adapter failed to open call: {info}"})
      await server.close()
      await browser.close()
      return
  except (TimeoutError, ConnectionClosed) as e:
    await send({"type": "error", "message": f"Adapter closed during open handshake ({type(e).__name__})."})
    await server.close()
    await browser.close()
    return

  print(f"call {conversation_id}: Genesys adapter returned 'opened' (connected to maxis-voice-agent)", flush=True)
  await send({"type": "caller", "msisdn": msisdn})
  await send({"type": "server", "url": SERVER_URL, "conversation_id": conversation_id})
  playback = {"until": 0.0}

  async def upstream() -> str:
    async for raw in browser:
      msg = _parse(raw)
      kind = msg.get("type")
      if kind == "audio":
        pcm = _b64(msg.get("data"))
        if pcm:
          ulaw = pcm16_to_ulaw(resample_pcm16(pcm, MIC_RATE, ces.RATE))
          for i in range(0, len(ulaw), ces.FRAME):
            await server.send(ulaw[i : i + ces.FRAME])
      elif kind == "ping":
        await send({"type": "pong"})
      elif kind == "end":
        break
    try:
      await send_genesys("close", {"reason": "end"})
    except ConnectionClosed:
      pass
    return "caller hung up"

  async def downstream() -> str:
    async for raw in server:
      if isinstance(raw, bytes):
        playback["until"] = max(playback["until"], time.monotonic()) + len(raw) / ces.RATE
        pcm = resample_pcm16(ulaw_to_pcm16(raw), ces.RATE, SPEAKER_RATE)
        await send({"type": "audio", "data": base64.b64encode(pcm).decode()})
        continue
      msg = _parse(raw)
      seq["server"] = msg.get("seq", seq["server"])
      mtype = msg.get("type")
      if mtype == "ping":
        await send_genesys("pong")
      elif mtype == "disconnect":
        params = (msg.get("parameters") or {}).get("outputVariables") or {}
        reason = (msg.get("parameters") or {}).get("reason", "completed")
        info = (msg.get("parameters") or {}).get("info", "")
        print(f"call {conversation_id}: adapter disconnect reason={reason} info={info} outputVariables={params}", flush=True)
        await send({"type": "session_end", "params": params})
        try:
          await send_genesys("close", {"reason": "end"})
        except ConnectionClosed:
          pass
        await asyncio.sleep(min(max(0.0, playback["until"] - time.monotonic()) + 0.5, MAX_GOODBYE_WAIT_S))
        return f"adapter disconnect ({reason}) {params}"
      elif mtype == "closed":
        return "adapter closed"
    return f"adapter socket closed ({server.close_code})"

  tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
  try:
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in done:
      exc = task.exception()
      if exc and not isinstance(exc, ConnectionClosed):
        raise exc
      print(f"call {conversation_id}: {task.result() if not exc else 'connection closed'}", flush=True)
  except Exception as e:  # noqa: BLE001
    print(f"call {conversation_id} failed: {e!r}", flush=True)
    try:
      await send({"type": "error", "message": "The test call failed (see client terminal)."})
    except ConnectionClosed:
      pass
  finally:
    for task in tasks:
      task.cancel()
    await server.close()
    await browser.close()


async def _handle_browser_direct_ces(browser):
  send = lambda payload: browser.send(json.dumps(payload))  # noqa: E731

  # 1. Browser handshake: {"type":"init","timezone","msisdn"}.
  init = await _wait_for_init(browser)
  if init is None:
    return
  msisdn = ces.normalise_msisdn(init.get("msisdn"))
  if not msisdn:
    await send({"type": "error", "message": "Enter a valid caller number (8-15 digits)."})
    await browser.close()
    return
  variables = {"msisdn": f"tel:+{msisdn}", "debug_events": "true"}  # like Architect: msisdn = Call.Ani
  timezone = str(init.get("timezone") or "")[:64]
  if timezone:
    variables["timezone"] = timezone
  conversation_id = str(uuid.uuid4())

  # 2. Dial the server like the Genesys adapter does.
  server = await _connect_server(send)
  if server is None:
    await browser.close()
    return
  print(f"call {conversation_id}: caller ...{msisdn[-4:]} -> {SERVER_URL}", flush=True)
  playback = {"until": 0.0}  # when the browser will finish playing the bot audio sent so far

  async def to_server(message: dict):
    await server.send(json.dumps(message))

  try:
    for m in ces.opening_messages(conversation_id, variables):  # config, variables, session_start
      await to_server(m)
  except ConnectionClosed:
    await send({"type": "error", "message": "Server closed the call during setup."})
    await browser.close()
    return
  await send({"type": "caller", "msisdn": msisdn})
  await send({"type": "server", "url": SERVER_URL, "conversation_id": conversation_id})

  # 3. Browser -> server: mic audio (PCM16 16k -> MULAW 8k, 20 ms frames), typed text.
  async def upstream() -> str:
    async for raw in browser:
      msg = _parse(raw)
      kind = msg.get("type")
      if kind == "audio":
        pcm = _b64(msg.get("data"))
        if pcm:
          for m in ces.audio_messages(pcm16_to_ulaw(resample_pcm16(pcm, MIC_RATE, ces.RATE))):
            await to_server(m)
      elif kind == "text":
        text = str(msg.get("data") or "").strip()[:MAX_TEXT_LEN]
        if text:
          await to_server({"realtimeInput": {"text": text}})
      elif kind == "ping":
        await send({"type": "pong"})
      elif kind == "end":
        break
    # "Stop" pressed or the tab closed: the caller hangs up.
    for m in ces.hangup_messages():
      await to_server(m)
    return "caller hung up"

  # 4. Server -> browser: bot audio (MULAW 8k -> PCM16 24k), barge-in, debug events, end.
  async def downstream() -> str:
    async for raw in server:
      msg = _parse(raw)
      out = msg.get("sessionOutput")
      if isinstance(out, dict) and "audio" in out:
        ulaw = _b64(out["audio"])
        if ulaw:
          playback["until"] = max(playback["until"], time.monotonic()) + len(ulaw) / ces.RATE
          pcm = resample_pcm16(ulaw_to_pcm16(ulaw), ces.RATE, SPEAKER_RATE)
          await send({"type": "audio", "data": base64.b64encode(pcm).decode()})
      elif "interruptionSignal" in msg:
        playback["until"] = 0.0
        await send({"type": "interrupted"})
      elif isinstance(msg.get("debugEvent"), dict):
        event = dict(msg["debugEvent"])
        kind = event.pop("type", None)
        if kind in DEBUG_KINDS:
          await send({"type": kind, **event})
      elif isinstance(msg.get("endSession"), dict):
        params = (msg["endSession"].get("metadata") or {}).get("params") or {}
        await send({"type": "session_end", "params": params})  # = Genesys outputVariables
        # Like the adapter: play out the queued goodbye before disconnecting.
        await asyncio.sleep(min(max(0.0, playback["until"] - time.monotonic()) + 0.5, MAX_GOODBYE_WAIT_S))
        return f"session ended {params}"
    code = server.close_code
    if code not in (None, 1000):
      await send({"type": "error", "message": f"Server closed the call (code {code}: {server.close_reason or 'error'})."})
    return f"server closed ({code})"

  tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
  try:
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in done:
      exc = task.exception()
      if exc and not isinstance(exc, ConnectionClosed):
        raise exc
      print(f"call {conversation_id}: {task.result() if not exc else 'connection closed'}", flush=True)
  except Exception as e:  # noqa: BLE001 - local test tool: show the reason
    print(f"call {conversation_id} failed: {e!r}", flush=True)
    try:
      await send({"type": "error", "message": "The test call failed (see client terminal)."})
    except ConnectionClosed:
      pass
  finally:
    for task in tasks:
      task.cancel()
    await server.close()
    await browser.close()


async def _wait_for_init(browser) -> dict | None:
  try:
    async with asyncio.timeout(INIT_TIMEOUT_S):
      while True:
        msg = _parse(await browser.recv())
        if msg.get("type") == "init":
          return msg
  except (TimeoutError, ConnectionClosed):
    return None


async def _connect_server(send):
  """Opens the WebSocket to either the Genesys Adapter or the /ces Server directly."""
  local = urlparse(SERVER_URL).hostname in ("127.0.0.1", "localhost")
  try:
    if IS_GENESYS_ADAPTER:
      headers = await asyncio.to_thread(_genesys_signed_headers, SERVER_URL)
    else:
      headers = {"Authorization": f"Bearer {TOKEN}", **await asyncio.to_thread(_cloud_run_headers)}
    server = await connect(SERVER_URL, additional_headers=headers,
                           open_timeout=CONNECT_TIMEOUT_S, max_size=4 << 20,
                           **({"proxy": None} if local else {}))
  except RuntimeError as e:  # gcloud identity token / secret
    message = str(e)
  except InvalidStatus as e:
    status = e.response.status_code
    hint = {401: "wrong token/signature, or Cloud Run IAM rejected your identity token",
            403: "your account lacks run.invoker on the Cloud Run service", 404: "wrong SERVER_URL path"}
    message = f"Server refused the call: HTTP {status} ({hint.get(status, 'see server logs')})."
  except (OSError, InvalidHandshake, InvalidURI, TimeoutError) as e:
    message = f"Could not reach {SERVER_URL} ({type(e).__name__})."
  else:
    return server
  print(message, flush=True)
  await send({"type": "error", "message": message})
  return None


def _parse(raw) -> dict:
  if not isinstance(raw, str):
    return {}
  try:
    data = json.loads(raw)
  except json.JSONDecodeError:
    return {}
  return data if isinstance(data, dict) else {}


def _b64(raw) -> bytes:
  if not isinstance(raw, str) or len(raw) > MAX_B64_LEN:
    return b""
  try:
    return base64.b64decode(raw, validate=True)
  except (binascii.Error, ValueError):
    return b""


async def main():
  if IS_GENESYS_ADAPTER:
    await asyncio.to_thread(_ensure_genesys_creds)
    await asyncio.to_thread(_get_id_token)
    mode = "Genesys Audio Connector v2 -> Cloud Run Adapter -> Cloud Run Voice Agent"
  else:
    if len(TOKEN) < MIN_TOKEN_LEN:
      raise SystemExit("Set CES_BRIDGE_TOKEN in voice_agent_client/.env (the same token the server uses).")
    mode = "Direct CES protocol"
  async with serve(handle_browser, HOST, PORT, process_request=process_request,
                   ping_interval=30, max_size=1 << 20) as srv:
    print(f"Test client on http://{HOST}:{PORT}  ->  {SERVER_URL}  [{mode}]", flush=True)
    await srv.serve_forever()


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    pass
