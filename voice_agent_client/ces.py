"""CES BidiRunSession messages, exactly as the ces-genesys-adapter sends them.

Shared by client.py (browser test client) and ces_adapter_sim.py (WAV-driven test),
so both talk to the server like Genesys does. Audio on the wire: MULAW 8 kHz.
"""

import base64
import json
import os
import re

FRAME = 160                  # 20 ms of MULAW 8 kHz (what the adapter sends)
SILENCE = b"\xff" * FRAME
RATE = 8000
DISCONNECT_EVENT = "sys.remote-call-disconnected"


def load_token() -> str:
  """CES_BRIDGE_TOKEN: the raw token, or the adapter's secret JSON {"access_token": ...}."""
  raw = os.getenv("CES_BRIDGE_TOKEN", "").strip()
  if raw.startswith("{"):
    try:
      raw = str(json.loads(raw).get("access_token") or "").strip()
    except (json.JSONDecodeError, AttributeError):
      raw = ""
  return raw


def normalise_msisdn(raw) -> str:
  """'+60 16-412 9768' / '016-412 9768' -> '60164129768'. '' if not 8-15 digits."""
  digits = re.sub(r"\D", "", str(raw or ""))
  if digits.startswith("0"):
    digits = "6" + digits
  return digits if re.fullmatch(r"[0-9]{8,15}", digits) else ""


def opening_messages(conversation_id: str, variables: dict) -> list[dict]:
  """config -> variables -> session_start: the adapter's first three messages."""
  return [
      {"config": {"session": f"projects/p/locations/us/apps/adk/sessions/{conversation_id}",
                  "inputAudioConfig": {"audioEncoding": "MULAW", "sampleRateHertz": RATE},
                  "outputAudioConfig": {"audioEncoding": "MULAW", "sampleRateHertz": RATE}}},
      {"realtimeInput": {"variables": {**variables, "adapterSessionId": conversation_id}}},
      {"realtimeInput": {"event": {"event": "session_start"}}},
  ]


def audio_messages(ulaw: bytes) -> list[dict]:
  """MULAW bytes -> one realtimeInput.audio message per 20 ms frame."""
  return [{"realtimeInput": {"audio": base64.b64encode(ulaw[i:i + FRAME].ljust(FRAME, b"\xff")).decode()}}
          for i in range(0, len(ulaw), FRAME)]


def hangup_messages() -> list[dict]:
  """Caller hung up: the adapter sends the disconnect event, then half-closes."""
  return [{"realtimeInput": {"event": {"event": DISCONNECT_EVENT}}}, {"clientHalfClose": True}]
