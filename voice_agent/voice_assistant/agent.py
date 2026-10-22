"""Maxis voice agent: orchestrator with the FAQ search tool.

The roaming sub-agent will be added later (the prompt already routes to
roaming_agent). `create_root_agent(ctx)` builds a fresh agent per client and
injects today's date. `root_agent` keeps `adk web` working.
"""

import os
from datetime import datetime

from google.adk.agents import Agent

from guardrails import generate_content_config

from .faq_search_tool import search_knowledge_base
from .prompts import orchestrator_instruction

LIVE_MODEL = os.getenv("VOICE_AGENT_MODEL", "gemini-3.8-live")


def create_root_agent(ctx: dict | None = None) -> Agent:
  today = (ctx or {}).get("date") or datetime.now().strftime("%Y-%m-%d")
  return Agent(
      name="maxis_agent",
      model=LIVE_MODEL,
      description="Maxis virtual agent: greets callers, answers general Maxis questions.",
      instruction=orchestrator_instruction(today),
      tools=[search_knowledge_base],
      generate_content_config=generate_content_config(),  # Gemini safety filters (AI-03)
      # sub_agents=[roaming_agent],  # TODO: add when the roaming flow is built
  )


root_agent = create_root_agent()
