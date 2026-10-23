"""Roaming and WhatsApp tools: one function per gateway operation in roaming.yaml
and messages.yaml, named exactly like the operationIds.

The calling number comes from session state (`caller_id`, set by the channel:
Genesys ANI, or the test-caller picker in the browser) and goes to the gateway
only as the X-Caller-Id header. It is never a tool argument, so the model can
neither see nor change whose account is used. Every answer is the gateway's
envelope; the agent follows responseData.next_step.
"""

from __future__ import annotations

from typing import Literal

from google.adk.tools import ToolContext

from . import gateway_client


def _ids(tool_context: ToolContext) -> dict:
  return {"caller_id": tool_context.state.get("caller_id") or None,
          "conversation_id": tool_context.session.id}


async def listRoamingLines(tool_context: ToolContext) -> dict:  # noqa: N802 (operationId)
  """Lines the caller can manage for roaming: their own line and, for a principal
  line, its share lines, with whether roaming is on and whether each can buy a pass.
  Use it when the caller means a line other than their own, or to check whose line
  a purchase or switch is for. Then pass that line's line_ref to other tools.
  """
  return await gateway_client.request("GET", "/roaming/lines", **_ids(tool_context))


async def findPasses(  # noqa: N802
    destinations: str,
    tool_context: ToolContext,
    start_date: str | None = None,
    end_date: str | None = None,
    days: int | None = None,
    line_ref: str | None = None,
) -> dict:
  """Finds roaming passes for a trip. Without a start date it lists options (for
  "how much is roaming in Japan?": answer, don't push a purchase). With a start
  date and an end date or number of days it recommends the best pass. Mention any
  notices before recommending. Follow responseData.next_step.

  Args:
    destinations: Places as the caller said them, in English, comma-separated, one
      per place, e.g. "Tokyo, Seoul". Cities are fine.
    start_date: First day of the trip, YYYY-MM-DD. Leave out if the caller hasn't said.
    end_date: Last day of the trip, YYYY-MM-DD. Prefer this over days when the caller gives dates.
    days: Trip length in days, when the caller gives a length rather than an end date.
    line_ref: The line the pass is for, from listRoamingLines. Leave out for the caller's own line.
  """
  return await gateway_client.request(
      "GET", "/roaming/passes", **_ids(tool_context),
      params={"destinations": destinations, "start_date": start_date, "end_date": end_date,
              "days": days, "line_ref": line_ref})


async def buyPass(  # noqa: N802
    pass_id: str,
    destinations: str,
    start_date: str,
    tool_context: ToolContext,
    line_ref: str | None = None,
    confirmed: bool = False,
) -> dict:
  """Buys a roaming pass, in two steps. First call without confirmed: it returns a
  summary to read back (pass, price, start, line, and whether roaming will be
  switched on first). Only after the caller clearly says yes, call again with
  confirmed true and the same values. A start date of today buys now; a later date
  books it ahead. Never say the pass is bought until next_step is purchase_done.

  Args:
    pass_id: pass_id from findPasses.
    destinations: The same destinations used in findPasses.
    start_date: The day the pass starts, YYYY-MM-DD. Today's date buys it now.
    line_ref: The line it's for, from listRoamingLines. Leave out for the caller's own line.
    confirmed: True only after the caller has agreed to the summary.
  """
  return await gateway_client.request(
      "POST", "/roaming/purchases", **_ids(tool_context),
      body={"pass_id": pass_id, "destinations": destinations, "start_date": start_date,
            "line_ref": line_ref, "confirmed": bool(confirmed)})


async def listMyPasses(  # noqa: N802
    tool_context: ToolContext,
    when: Literal["scheduled", "active", "past", "all"] = "all",
    line_ref: str | None = None,
) -> dict:
  """The caller's roaming passes: scheduled, active and past (last 60 days). Use it
  when the caller asks about their passes (what they have, when one starts or ends)
  or bought one by mistake; pass_ref identifies a pass for helpWithWrongPass.

  Args:
    when: scheduled (not started yet: "haven't flown yet"), active (running now:
      "I'm overseas"), past (finished: "already back"), or all when unclear.
    line_ref: The line, from listRoamingLines. Leave out for the caller's own line.
  """
  return await gateway_client.request(
      "GET", "/roaming/purchases", **_ids(tool_context), params={"when": when, "line_ref": line_ref})


async def helpWithWrongPass(  # noqa: N802
    pass_ref: str,
    tool_context: ToolContext,
    line_ref: str | None = None,
) -> dict:
  """What can be done about a pass bought by mistake. Applies Maxis's rule: a
  scheduled pass can be cancelled by the customer in the Maxis app up to 2 hours
  before it starts. You never cancel and never refund; anything else goes to a person.

  Args:
    pass_ref: pass_ref from listMyPasses.
    line_ref: The line, from listRoamingLines. Leave out for the caller's own line.
  """
  return await gateway_client.request(
      "POST", "/roaming/wrong-pass", **_ids(tool_context), body={"pass_ref": pass_ref, "line_ref": line_ref})


async def switchRoaming(  # noqa: N802
    turn: Literal["on", "off"],
    tool_context: ToolContext,
    line_ref: str | None = None,
    confirmed: bool = False,
    acknowledged_active_pass: bool = False,
) -> dict:
  """Switches international roaming on or off, in two steps like buyPass: without
  confirmed it returns what will happen; after the caller says yes to that exact
  action, call again with confirmed true. Switching off while a pass is active or
  scheduled first returns a warning; only continue if the caller still wants it
  (acknowledged_active_pass true). A change takes about 5 minutes.

  Args:
    turn: "on" or "off".
    line_ref: The line, from listRoamingLines. Leave out for the caller's own line.
    confirmed: True only after the caller has agreed to this exact action.
    acknowledged_active_pass: True when the caller heard the warning about their
      passes and still wants roaming off.
  """
  return await gateway_client.request(
      "POST", "/roaming/switch", **_ids(tool_context),
      body={"turn": turn, "line_ref": line_ref, "confirmed": bool(confirmed),
            "acknowledged_active_pass": bool(acknowledged_active_pass)})


async def sendWhatsAppLink(kind: Literal["manage_passes", "wrong_pass", "insurance"], tool_context: ToolContext) -> dict:  # noqa: N802
  """Sends one approved WhatsApp message to the caller's own number, at most once
  per call for each kind. Only send it after the caller agrees. The messages are
  fixed and in English. Follow responseData.next_step.

  Args:
    kind: manage_passes (link to manage or cancel scheduled roaming passes in the
      Maxis app), wrong_pass (help with a wrongly bought roaming pass), or
      insurance (the free travel insurance offer).
  """
  return await gateway_client.request("POST", "/messages/whatsapp", **_ids(tool_context), body={"kind": kind})


ROAMING_TOOLS = [listRoamingLines, findPasses, buyPass, listMyPasses, helpWithWrongPass, switchRoaming,
                 sendWhatsAppLink]
