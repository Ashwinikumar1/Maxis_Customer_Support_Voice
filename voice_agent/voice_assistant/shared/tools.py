"""Tools any agent can use: WhatsApp links, escalation to a person, ending the call.

Function names match the names used in the prompts (sendWhatsAppLink, escalateToAgent).

- sendWhatsAppLink -> gateway POST /messages/whatsapp (gw_messages spec).
- escalateToAgent and endCall don't call the gateway (it has no handoff API): they
  record a call outcome in session state and the channel acts on it when the agent's
  reply is finished (browser: shown in the UI; Genesys: "disconnect" with the outcome
  as outputVariables, so the Architect flow transfers to a queue).
endCall is NOT given to any agent for now (disabled on request); it's kept ready for
telephony. To enable it: add it to the agents' tools and add a closing rule to the prompt.
"""

from typing import Literal, Optional

from google.adk.tools import ToolContext

from ..gateway import call_gateway, ok, post_disabled, safe_tool  # noqa: F401 (call_gateway: POST re-enable)

CALL_OUTCOME_KEY = "call_outcome"  # read by streaming_core.call (same key)


@safe_tool
async def sendWhatsAppLink(  # noqa: N802
    kind: Literal["manage_passes", "wrong_pass", "insurance"], tool_context: ToolContext) -> dict:
  """Sends one approved WhatsApp message to the caller's own number, at most once per
  call for each kind. Only send it after the caller agrees. The number is added by
  the platform. Follow responseData.next_step.

  Args:
    kind: manage_passes (link to manage or cancel scheduled roaming passes in the
      Maxis app), wrong_pass (help with a wrongly bought roaming pass) or insurance
      (the free travel insurance offer).
  """
  # TEMPORARY: POST disabled while testing on the PROD gateway (would message a real customer).
  # return await call_gateway("sendWhatsAppLink", "POST", "/messages/whatsapp",
  #                           tool_context, body={"kind": kind})
  return post_disabled()


@safe_tool
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
  params = {"reason": (reason or "")[:200], "customer_request": (customer_request or "")[:500],
            "use_case": use_case, "destination": destination, "pass_name": pass_name}
  _set_outcome(tool_context, {"action": "handoff", **{k: v for k, v in params.items() if v}})
  return ok({"next_step": "tell_caller_connecting"})


@safe_tool
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
