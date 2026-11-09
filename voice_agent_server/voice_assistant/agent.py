"""Maxis voice agent: maxis_agent answers Maxis FAQ questions by voice and hands
account tasks to the specialist flows (flows/, e.g. roaming_agent).

The FAQ is ADK's built-in VertexAiSearchTool on the Maxis voicebot data store
(FAQ_DATASTORE_ID), grounded natively inside the Gemini Live turn. The flows' tools
call the Maxis agent gateway (GATEWAY_URL) with X-Caller-Id = the caller's MSISDN.
`create_root_agent(ctx)` builds a fresh agent per call and injects today's date.
`root_agent` keeps `adk web` working.
"""

import logging
import os
import threading
from datetime import datetime

import google.auth
from google.auth.transport.requests import Request
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.tools import VertexAiSearchTool

from .flows import FLOWS
from .prompts import orchestrator_instruction

LIVE_MODEL = os.getenv("VOICE_AGENT_MODEL", "gemini-3.8-live")
FAQ_DATASTORE_ID = os.getenv(
    "FAQ_DATASTORE_ID",
    "projects/mxs-maxisapp-prd/locations/global/collections/default_collection"
    "/dataStores/voicebot-maxis-roaming-faq_1782955089227",
)
_CREDS_REFRESH_S = 45 * 60   # Google access tokens last 60 min: refresh well before
_log = logging.getLogger("voice_agent")

# --- One model object for every call --------------------------------------------
# By default ADK builds a new genai client per call (agents are rebuilt per call), and
# each new client fetches Google credentials before the Live session opens: measured
# 6-7 s on a laptop, i.e. silence before the greeting. One shared Gemini with shared
# credentials (loaded at startup by warm_up_model, refreshed in the background) removes
# that from every call. ADK keeps one client per event loop, so sharing is safe.
_creds = None
if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true"):
  try:
    _creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
  except google.auth.exceptions.DefaultCredentialsError:
    _log.warning("No Google credentials found: each call will look them up itself")
_MODEL = Gemini(model=LIVE_MODEL, client_kwargs={"credentials": _creds} if _creds else None)


def warm_up_model() -> None:
  """Loads the shared credentials now and keeps them fresh (call once at startup)."""
  if _creds is None:
    return

  def refresh_forever():
    while True:
      try:
        _creds.refresh(Request())
        _log.info("Vertex AI credentials refreshed")
      except Exception:  # noqa: BLE001 - a failed refresh is retried; calls refresh on demand
        _log.exception("Vertex AI credentials refresh failed")
      threading.Event().wait(_CREDS_REFRESH_S)

  threading.Thread(target=refresh_forever, name="vertex-creds", daemon=True).start()


def create_root_agent(ctx: dict | None = None) -> Agent:
  today = (ctx or {}).get("date") or datetime.now().strftime("%Y-%m-%d")
  return Agent(
      name="maxis_agent",
      model=_MODEL,
      description="Maxis virtual agent: greets callers, answers general Maxis questions, routes.",
      instruction=orchestrator_instruction(today),
      tools=[VertexAiSearchTool(data_store_id=FAQ_DATASTORE_ID)],
      # One fresh specialist per call: an ADK agent can belong to only one parent.
      sub_agents=[flow.build_agent(today, _MODEL) for flow in FLOWS],
  )


root_agent = create_root_agent()
