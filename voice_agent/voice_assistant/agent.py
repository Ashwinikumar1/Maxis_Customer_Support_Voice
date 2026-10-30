"""Maxis voice agent: a small router (maxis_agent) plus specialist flows.

maxis_agent greets, answers general questions with the FAQ search tool and
transfers to a specialist (chat mode) such as roaming_agent. Specialists are
registered in flows/FLOWS. `create_root_agent(ctx)` builds a fresh agent tree per
call and injects today's date. `root_agent` keeps `adk web` working.
"""

import os
from datetime import datetime

from google.adk.agents import Agent

from .faq_search_tool import search_knowledge_base
from .flows import FLOWS
from .prompts import orchestrator_instruction

LIVE_MODEL = os.getenv("VOICE_AGENT_MODEL", "gemini-3.8-live")


def create_root_agent(ctx: dict | None = None) -> Agent:
  today = (ctx or {}).get("date") or datetime.now().strftime("%Y-%m-%d")
  return Agent(
      name="maxis_agent",
      model=LIVE_MODEL,
      description="Maxis virtual agent: greets callers, answers general Maxis questions, routes.",
      instruction=orchestrator_instruction(today),
      tools=[search_knowledge_base],
      # One fresh specialist per call: an ADK agent can belong to only one parent.
      sub_agents=[flow.build_agent(today, LIVE_MODEL) for flow in FLOWS],
  )


root_agent = create_root_agent()
