"""Tools any agent can use: WhatsApp links, escalation to a person, ending the call.

Function names match the names used in the prompts (sendWhatsAppLink, escalateToAgent).
escalateToAgent records a call outcome in session state; the channel (browser,
Genesys) acts on it when the agent's reply is finished.
endCall is NOT given to any agent for now (disabled on request); it's kept ready for
telephony. To enable it: add it to the agents' tools and add a closing rule to the prompt.
"""

from typing import Literal, Optional

from google.adk.tools import ToolContext

from ..gateway import call_gateway
from . import mock_backend  # noqa: F401  registers dummy handlers (used when GATEWAY_URL is unset)

CALL_OUTCOME_KEY = "call_outcome"  # read by streaming_core.call (same key)


async def sendWhatsAppLink(  # noqa: N802
    kind: Literal["insurance", "manage_passes"], tool_context: ToolContext) -> dict:
  """Sends the caller a WhatsApp message with a link. Only call it after the caller
  has said yes to receiving it. The caller's number is added by the platform.

  Args:
    kind: insurance (the free travel insurance details) or manage_passes (the link
      to manage roaming passes in the Maxis app).
  """
  return await call_gateway("sendWhatsAppLink", "POST", "/messaging/whatsapp-link",
                            tool_context, body={"kind": kind})


async def escalateToAgent(  # noqa: N802
    reason: str,
    customer_request: str,
    tool_context: ToolContext,
    use_case: Optional[str] = None,
    destination: Optional[str] = None,
    pass_name: Optional[str] = None,
) -> dict:
  """Hands the call to a person, with context so the caller doesn't repeat themselves.
  Only after the caller has agreed to be put through.

  Args:
    reason: Why a person is needed, in a few words.
    customer_request: What the customer wanted but couldn't get done.
    use_case: For roaming: buy_a_pass, pass_bought_by_mistake or roaming_on_off.
    destination: The trip destination, when known.
    pass_name: The pass involved, when known.
  """
  body = {"reason": reason[:200], "customer_request": customer_request[:500],
          "use_case": use_case, "destination": destination, "pass_name": pass_name}
  result = await call_gateway("escalateToAgent", "POST", "/handoff/agent", tool_context, body=body)
  if result.get("status") == "success":
    # The channel hands the call over once this reply is finished
    # (Genesys: "disconnect" with these as outputVariables -> Architect routes to a queue).
    _set_outcome(tool_context, {"action": "handoff", **{k: v for k, v in body.items() if v}})
  return result


async def endCall(  # noqa: N802
    reason: Literal["resolved", "caller_done", "no_response", "unsupported_language"],
    tool_context: ToolContext,
) -> dict:
  """Ends the call. Only after you've said a warm goodbye and the caller needs
  nothing else. Never after escalateToAgent (that hands the call over already).

  Args:
    reason: resolved (helped them), caller_done (they're finished), no_response
      (silence after a check-in), or unsupported_language.
  """
  _set_outcome(tool_context, {"action": "end", "reason": reason})
  return {"status": "success", "responseData": {"next_step": "say_nothing_more"}}


def _set_outcome(tool_context: ToolContext, outcome: dict):
  """Records the call outcome once (a handoff is never overwritten by an end)."""
  if not tool_context.state.get(CALL_OUTCOME_KEY):
    tool_context.state[CALL_OUTCOME_KEY] = outcome
