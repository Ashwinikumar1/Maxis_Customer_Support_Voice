"""Maxis voice agent tree: orchestrator -> roaming specialist + handoff.

    maxis_agent (root)    greets, answers from the FAQ, routes          search_knowledge_base, end_session
    ├── roaming_agent     passes, wrong pass, roaming on/off, status    roaming + WhatsApp gateway tools, end_session
    └── handoff_agent     a person, out-of-scope account tasks, abuse   end_session

The caller hears one voice; ADK's transfer_to_agent moves the call between
agents (sub-agents can transfer back to their parent). `create_root_agent(ctx)`
builds a fresh tree per call with today's date in Malaysia time.
ROUTING_MODE (faq_first | specialist) picks the orchestrator's roaming routing,
see prompts.py. `root_agent` keeps `adk web` working.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from google.adk.agents import Agent

from . import prompts
from .faq_search_tool import search_knowledge_base
from .roaming_tools import ROAMING_TOOLS
from .session_tools import end_session

LIVE_MODEL = os.getenv("VOICE_AGENT_MODEL", "gemini-3.8-live")
MYT = ZoneInfo("Asia/Kuala_Lumpur")


def routing_mode() -> str:
  mode = os.getenv("ROUTING_MODE", "faq_first").strip().lower()
  return mode if mode in ("faq_first", "specialist") else "faq_first"


def create_root_agent(ctx: dict | None = None, model: str | None = None) -> Agent:
  """Builds the agent tree. `model` overrides the live model (the text test runner uses this)."""
  model = model or LIVE_MODEL
  now = datetime.now(MYT)  # roaming dates are always Malaysia time, whatever the caller's timezone
  today, today_spoken = now.strftime("%Y-%m-%d"), f"{now.strftime('%A')}, {now.day} {now.strftime('%B %Y')}"

  roaming_agent = Agent(
      name=prompts.ROAMING,
      model=model,
      description=("Roaming on the caller's own line: finds and buys roaming passes (now or ahead, for their "
                   "line or a share line), helps with a pass bought by mistake, switches international roaming "
                   "on or off, and checks roaming status and passes."),
      instruction=prompts.roaming_instruction(today, today_spoken),
      tools=[*ROAMING_TOOLS, end_session],
      disallow_transfer_to_peers=True,
  )
  handoff_agent = Agent(
      name=prompts.HANDOFF,
      model=model,
      description=("Requests this line can't handle (bills, payments, contracts, plan changes, devices, "
                   "complaints, home internet, mobile data), requests for a person, abusive callers. "
                   "Escalates to a person or ends the call."),
      instruction=prompts.handoff_instruction(today, today_spoken),
      tools=[end_session],
      disallow_transfer_to_peers=True,
  )
  return Agent(
      name=prompts.ORCHESTRATOR,
      model=model,
      description="Maxis virtual agent: greets callers, answers general Maxis questions, routes.",
      instruction=prompts.orchestrator_instruction(today, today_spoken, routing_mode()),
      tools=[search_knowledge_base, end_session],
      sub_agents=[roaming_agent, handoff_agent],
  )


root_agent = create_root_agent()
