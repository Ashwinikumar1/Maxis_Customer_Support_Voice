"""Maxis voice agent: maxis_agent answers Maxis FAQ questions by voice and hands
account tasks to the specialist flows (flows/, e.g. roaming_agent).

The FAQ is ADK's built-in VertexAiSearchTool on the Maxis voicebot data store
(FAQ_DATASTORE_ID), grounded natively inside the Gemini Live turn. The flows' tools
call the Maxis agent gateway (GATEWAY_URL) with X-Caller-Id = the caller's MSISDN.
`create_root_agent(ctx)` builds a fresh agent per call and injects today's date.
`root_agent` keeps `adk web` working.
"""

import os
from datetime import datetime

from google.adk.agents import Agent
from google.adk.tools import VertexAiSearchTool

from .flows import FLOWS
from .prompts import orchestrator_instruction

LIVE_MODEL = os.getenv("VOICE_AGENT_MODEL", "gemini-3.8-live")
FAQ_DATASTORE_ID = os.getenv(
    "FAQ_DATASTORE_ID",
    "projects/mxs-maxisapp-prd/locations/global/collections/default_collection"
    "/dataStores/voicebot-maxis-roaming-faq_1782955089227",
)


def create_root_agent(ctx: dict | None = None) -> Agent:
  today = (ctx or {}).get("date") or datetime.now().strftime("%Y-%m-%d")
  return Agent(
      name="maxis_agent",
      model=LIVE_MODEL,
      description="Maxis virtual agent: greets callers, answers general Maxis questions, routes.",
      instruction=orchestrator_instruction(today),
      tools=[VertexAiSearchTool(data_store_id=FAQ_DATASTORE_ID)],
      # One fresh specialist per call: an ADK agent can belong to only one parent.
      sub_agents=[flow.build_agent(today, LIVE_MODEL) for flow in FLOWS],
  )


root_agent = create_root_agent()
