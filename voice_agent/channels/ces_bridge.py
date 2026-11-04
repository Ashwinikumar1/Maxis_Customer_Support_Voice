"""CES-protocol bridge: lets the existing ces-genesys-adapter drive our ADK agent.

This is the SAME Genesys adapter GECX (Gemini Enterprise for CX / CX Agent Studio)
uses. We don't build a new Genesys integration: the adapter's backend URL points here
instead of to GECX, and this module answers the same CES protocol with custom ADK code.

The adapter (github.com/GoogleCloudPlatform/ces-genesys-adapter, already on Cloud
Run for Maxis) handles everything Genesys: API key + signature check, open/opened,
ping, DTMF, audio pacing, clearing buffered audio on barge-in, and the final
"disconnect" with outputVariables. Its backend speaks the CES BidiRunSession
WebSocket protocol. This module answers that protocol, so setting the adapter's
CES URL to  wss://<this service>/ces/  sends calls to our ADK agents instead of CES.

adapter -> us                                              us -> adapter
{"config":{"session":"<app>/sessions/<conversationId>"}}   {"sessionOutput":{"audio":b64 MULAW 8k}}
{"realtimeInput":{"variables":{...}}}  Architect inputs    {"interruptionSignal":{}}       barge-in
{"realtimeInput":{"event":{"event":"session_start"}}}      {"endSession":{"metadata":{"params":{}}}}
{"realtimeInput":{"text":"..."}}       optional kickstart     params -> Genesys outputVariables
{"realtimeInput":{"audio":b64 MULAW 8k}}
{"realtimeInput":{"dtmf":"1"}}
{"realtimeInput":{"event":{"event":"sys.remote-call-disconnected"}}}   caller hung up
{"clientHalfClose":true}

Auth: the adapter sends "Authorization: Bearer <token>". With the adapter's
AUTH_TOKEN_SECRET_PATH set, that token comes from a Secret Manager secret holding
{"access_token": "...", "expiry": <ms>}. The SAME secret is mounted here as
CES_BRIDGE_TOKEN, so no adapter auth code changes. Genesys itself is verified by
the adapter (API key + HTTP message signature) before it ever connects to us.
"""

import asyncio
import base64
import binascii
import hmac
import json
import os
import re
import uuid
from typing import Awaitable, Callable

from websockets.exceptions import ConnectionClosed

from streaming_core import Call, CallInfo, logger
from streaming_core.audio import pcm16_to_ulaw, resample_pcm16, ulaw_to_pcm16
from streaming_core.utils import RECEIVE_SAMPLE_RATE, SEND_SAMPLE_RATE

# --- Settings ----------------------------------------------------------------
ENABLED = os.getenv("CES_BRIDGE_ENABLED", "false").strip().lower() == "true"
# Text sent as the caller's first turn when the adapter sends "session_start", so the
# agent greets right away (on a phone line the caller waits silently).
KICKSTART_TEXT = os.getenv("CES_BRIDGE_KICKSTART_TEXT", "Hello").strip()
# Architect inputVariables holding the caller's number, in priority order.
MSISDN_VARIABLES = ("msisdn", "ani", "caller_id")
SETUP_TIMEOUT_S = 10.0          # config + session_start must arrive within this
MIN_TOKEN_LEN = 32
MAX_AUDIO_B64 = 256 * 1024      # one adapter audio frame is ~20-100 ms (a few KB)
MAX_VARIABLES, MAX_VAR_LEN = 50, 500
GENESYS_RATE = 8000             # adapter config: MULAW 8 kHz both ways

_PATH_RE = re.compile(r"/ces/[a-z0-9-]{1,40}")       # adapter appends the CES location
_SESSION_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_VAR_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_MSISDN_RE = re.compile(r"[0-9]{8,15}")
_DTMF = set("0123456789*#")
_SESSION_START = "session_start"

StartCall = Callable[[CallInfo], Awaitable[Call]]


def _load_token() -> str:
  """CES_BRIDGE_TOKEN: the raw token, or the adapter's secret JSON {"access_token": ...}."""
  raw = os.getenv("CES_BRIDGE_TOKEN", "").strip()
  if raw.startswith("{"):
    try:
      raw = str(json.loads(raw).get("access_token") or "").strip()
    except (json.JSONDecodeError, AttributeError):
      raw = ""
  return raw


_TOKEN = _load_token()
if ENABLED and len(_TOKEN) < MIN_TOKEN_LEN:
  # Fail closed: the endpoint stays mounted but rejects every connection.
  logger.error("CES bridge enabled but CES_BRIDGE_TOKEN is missing or shorter than %d chars:"
               " all /ces connections will be rejected.", MIN_TOKEN_LEN)


# --- HTTP handshake checks (called from server.process_request) ---------------
def is_bridge_path(path: str) -> bool:
  return bool(_PATH_RE.fullmatch(path))


def authorized(headers) -> bool:
  """Constant-time Bearer token check. Disabled or unconfigured -> always False."""
  if not ENABLED or len(_TOKEN) < MIN_TOKEN_LEN:
    return False
  scheme, _, token = (headers.get("Authorization") or "").partition(" ")
  return scheme.lower() == "bearer" and hmac.compare_digest(token.strip().encode(), _TOKEN.encode())


# --- One adapter connection == one phone call ----------------------------------
async def handle(websocket, start_call: StartCall):
  send = lambda payload: websocket.send(json.dumps(payload))  # noqa: E731

  # 1. Setup: config (session id), optional variables, then session_start / text.
  try:
    session_id, variables, kickstart = await asyncio.wait_for(_setup(websocket), SETUP_TIMEOUT_S)
  except (TimeoutError, ConnectionClosed, ValueError) as e:
    logger.warning("CES bridge: setup failed (%s)", type(e).__name__)
    await websocket.close(1008, "setup failed")
    return

  info = CallInfo(msisdn=_msisdn(variables), conversation_id=session_id,
                  channel="genesys", channel_vars=variables)
  try:
    call = await start_call(info)
  except Exception:
    logger.exception("CES bridge: call %s failed to start", session_id)
    await websocket.close(1011, "internal error")  # adapter -> disconnect(error) -> Architect failure path
    return
  logger.info("CES bridge: call %s started (msisdn %s, variables %s)", session_id,
              f"...{info.msisdn[-4:]}" if _MSISDN_RE.fullmatch(info.msisdn) else "unknown",
              sorted(variables))  # keys only: values may hold PII
  if kickstart:
    call.send_text(kickstart)

  # 2. Adapter -> model: caller audio, DTMF, hang-up.
  async def upstream() -> str:
    async for raw in websocket:
      msg = _parse(raw)
      if msg.get("clientHalfClose"):
        return "half_close"
      rt = msg.get("realtimeInput")
      if not isinstance(rt, dict):
        continue
      if "audio" in rt:
        ulaw = _b64(rt["audio"])
        if ulaw:
          call.send_audio(resample_pcm16(ulaw_to_pcm16(ulaw), GENESYS_RATE, SEND_SAMPLE_RATE))
      elif "dtmf" in rt:
        digit = str(rt["dtmf"])
        if digit in _DTMF:
          call.send_text(f"[The caller pressed {digit} on the keypad]")
      elif isinstance(rt.get("event"), dict) and rt["event"].get("event") != _SESSION_START:
        return "caller_hung_up"  # adapter's DISCONNECT_EVENT_NAME (sys.remote-call-disconnected)
    return "closed"

  # 3. Model -> adapter: bot audio (MULAW 8 kHz) and barge-in. The adapter paces the
  # audio to Genesys and clears its buffer on interruptionSignal.
  async def emit(kind, **data):
    if kind == "audio":
      ulaw = pcm16_to_ulaw(resample_pcm16(data["data"], RECEIVE_SAMPLE_RATE, GENESYS_RATE))
      if ulaw:
        await send({"sessionOutput": {"audio": base64.b64encode(ulaw).decode()}})
    elif kind == "interrupted":
      await send({"interruptionSignal": {}})

  async def wait_for_outcome() -> str:
    await call.ended.wait()  # endCall / escalateToAgent finished (see Call._check_outcome)
    return "outcome"

  tasks = {asyncio.create_task(upstream()): "upstream",
           asyncio.create_task(call.run(emit)): "model",
           asyncio.create_task(wait_for_outcome()): "outcome"}
  close_code, close_reason = 1000, "session ended"
  try:
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    task = done.pop()
    exc = task.exception()
    if exc and not isinstance(exc, ConnectionClosed):
      raise exc
    if tasks[task] == "model" and call.outcome is None:
      raise RuntimeError("live session ended without an outcome")
    # endSession: the adapter plays out the audio it has queued, then sends Genesys
    # "disconnect" (completed) with these params as outputVariables.
    params = output_variables(call.outcome) if call.outcome else {}
    await send({"endSession": {"metadata": {"params": params}}})
    logger.info("CES bridge: call %s ended (%s, outcome %s)", session_id,
                task.result() if not exc else "closed", params.get("outcome", "none"))
  except ConnectionClosed:
    pass  # adapter went away (Genesys hung up): nothing left to tell it
  except Exception:
    # Generic close only: details stay in our logs. The adapter turns 1011 into
    # disconnect(error), which the Architect flow should route to a queue.
    logger.exception("CES bridge: call %s failed", session_id)
    close_code, close_reason = 1011, "internal error"
  finally:
    for t in tasks:
      t.cancel()
    await call.finish()  # closes Live + saves the transcript once
    await websocket.close(close_code, close_reason)


async def _setup(websocket) -> tuple[str, dict, str]:
  """Reads the adapter's opening messages. Returns (session_id, variables, kickstart)."""
  session_id, variables = None, {}
  while True:
    msg = _parse(await websocket.recv())
    if isinstance(msg.get("config"), dict):
      session_id = _session_id(msg["config"].get("session"))
      continue
    rt = msg.get("realtimeInput")
    if not isinstance(rt, dict):
      continue
    if "variables" in rt:
      variables = _clean_variables(rt["variables"])
      continue
    if "text" in rt:      # adapter's _initial_message
      kickstart = rt["text"] if isinstance(rt["text"], str) else KICKSTART_TEXT
    elif isinstance(rt.get("event"), dict) and rt["event"].get("event") == _SESSION_START:
      kickstart = KICKSTART_TEXT
    elif "audio" in rt:   # no kickstart sent: start anyway, the caller's audio follows
      kickstart = ""
    else:
      continue
    if not session_id:
      raise ValueError("no config message before session start")
    return session_id, variables, kickstart[:500]


# --- Mapping helpers (all adapter data is untrusted) -----------------------------
def output_variables(outcome: dict) -> dict:
  """Call outcome (shared/tools.py) -> Genesys outputVariables (flat strings) for Architect."""
  action = str(outcome.get("action") or "")
  if action == "handoff":
    fields = {"handoff_reason": "reason", "summary": "customer_request", "use_case": "use_case",
              "destination": "destination", "pass_name": "pass_name"}
  else:
    fields = {"end_reason": "reason"}
  out = {"outcome": action}
  out.update({name: str(outcome[key])[:MAX_VAR_LEN] for name, key in fields.items() if outcome.get(key)})
  return out


def _session_id(session) -> str:
  """'<app>/sessions/<id>' -> '<id>' (the Genesys conversationId). Invalid -> random id."""
  tail = str(session or "").rsplit("/sessions/", 1)[-1]
  return tail if _SESSION_RE.fullmatch(tail) else uuid.uuid4().hex


def _clean_variables(raw) -> dict:
  if not isinstance(raw, dict):
    return {}
  out = {}
  for key, value in list(raw.items())[:MAX_VARIABLES]:
    if isinstance(key, str) and _VAR_KEY_RE.fullmatch(key) and isinstance(value, (str, int, float, bool)):
      out[key] = str(value)[:MAX_VAR_LEN]
  return out


def _msisdn(variables: dict) -> str:
  """Caller number from Architect variables (e.g. msisdn = Call.Ani 'tel:+6012...')."""
  for key in MSISDN_VARIABLES:
    digits = re.sub(r"\D", "", variables.get(key, ""))
    if _MSISDN_RE.fullmatch(digits):
      return digits
  # Withheld / unknown number: the agent still answers FAQs; gateway tools refuse
  # politely (gateway client checks the MSISDN format).
  return "anonymous"


def _parse(raw) -> dict:
  if not isinstance(raw, str):
    return {}
  try:
    data = json.loads(raw)
  except json.JSONDecodeError:
    return {}
  return data if isinstance(data, dict) else {}


def _b64(raw) -> bytes:
  if not isinstance(raw, str) or len(raw) > MAX_AUDIO_B64:
    return b""
  try:
    return base64.b64decode(raw, validate=True)
  except (binascii.Error, ValueError):
    return b""
