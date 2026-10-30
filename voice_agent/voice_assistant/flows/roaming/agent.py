"""Roaming specialist agent (chat mode: it keeps the caller for the whole roaming
conversation, then transfers back to maxis_agent or to a sibling flow)."""

from google.adk.agents import Agent

from ...faq_search_tool import search_knowledge_base
from ...shared.tools import escalateToAgent, sendWhatsAppLink
from .prompt import roaming_instruction
from .tools import ROAMING_TOOLS

NAME = "roaming_agent"
DESCRIPTION = (
    "Roaming on the caller's own line or their share lines: find and buy a roaming pass for a "
    "trip, help with a pass bought by mistake, check their passes, or switch international "
    "roaming on or off."
)


def build_agent(current_date: str, model: str) -> Agent:
  """A fresh roaming agent (ADK agents can have only one parent, so one per call)."""
  return Agent(
      name=NAME,
      model=model,  # must be a Live model: the whole call is one live session
      mode="chat",
      description=DESCRIPTION,
      instruction=roaming_instruction(current_date),
      tools=[*ROAMING_TOOLS, search_knowledge_base, sendWhatsAppLink, escalateToAgent],
  )
