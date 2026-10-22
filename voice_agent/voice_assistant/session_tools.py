"""end_session: ends the bot's part of the call, normally or with an escalation to a person.

Mirrors CX Agent Studio's built-in end_session used by every production agent.
The handover params become the Genesys output variables at disconnect (US-03,
US-33) once the Genesys channel is connected; in the browser test client the
server shows them as the handover card and writes them into the transcript.
"""

from __future__ import annotations

from typing import Literal

from google.adk.tools import ToolContext

from streaming_core.redaction import mask_phone_numbers

INTENTS = {"roaming", "faq", "other", "fibre_troubleshooting"}
USE_CASES = {"buy_a_pass", "pass_bought_by_mistake", "roaming_on_off", "roaming_status",
             "roaming_not_working", "roaming_charge_dispute"}


def _clean(value: str, limit: int = 500) -> str:
  """Free text for the handover: no phone numbers (Genesys already has the number), bounded length."""
  return mask_phone_numbers((value or "").strip(), "[number removed]")[:limit]


def end_session(
    session_escalated: bool,
    reason: str,
    tool_context: ToolContext,
    intent: Literal["roaming", "faq", "other"] = "other",
    summary: str = "",
    language: Literal["en", "ms"] = "en",
    use_case: str = "",
    destination: str = "",
    pass_name: str = "",
    customer_request: str = "",
) -> dict:
  """Ends your part of the call. In the same turn, BEFORE calling this, say your
  goodbye, or, when escalating, one short sentence that you're getting them to a
  person. Never call it in silence.

  Args:
    session_escalated: True to hand the call to a person; false when the caller is done.
    reason: One line on why the call is ending or escalating.
    intent: roaming, faq or other.
    summary: One or two plain English sentences: what happened and what the customer
      still needs, so the person never makes them repeat it. Required when escalating.
    language: en or ms, the language the caller was speaking at the end.
    use_case: Roaming only: buy_a_pass, pass_bought_by_mistake, roaming_on_off,
      roaming_status, roaming_not_working or roaming_charge_dispute.
    destination: Roaming only: where the caller is travelling, if known.
    pass_name: Roaming only: the pass involved, if any.
    customer_request: What the customer wanted but couldn't get done here.
  """
  params = {
      "intent": intent if intent in INTENTS else "other",
      "summary": _clean(summary),
      "language": language if language in ("en", "ms") else "en",
  }
  for key, value in (("use_case", use_case if use_case in USE_CASES else ""), ("destination", destination),
                     ("pass", pass_name), ("customer_request", customer_request)):
    if value:
      params[key] = _clean(value, 200)
  outcome = {"session_escalated": bool(session_escalated), "reason": _clean(reason, 200), "params": params,
             "agent": tool_context.agent_name}
  tool_context.state["end_session"] = outcome
  return {"status": "success", "next_step": "call_ending"}
