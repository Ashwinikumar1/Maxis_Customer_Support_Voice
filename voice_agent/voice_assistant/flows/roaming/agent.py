"""Roaming specialist agent (chat mode: it keeps the caller for the whole roaming
conversation, then transfers back to maxis_agent or to a sibling flow).

Always attached to maxis_agent as a sub-agent (see voice_assistant/agent.py).
Its tools call the Maxis agent gateway (gw_roaming / gw_messages specs). FAQ
questions go back to maxis_agent, which grounds on Vertex AI Search.
"""

from google.adk.agents import Agent

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
      tools=[*ROAMING_TOOLS, sendWhatsAppLink, escalateToAgent],
  )
