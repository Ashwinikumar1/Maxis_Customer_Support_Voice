"""Roaming tools: one function per OpenAPI operation, all via the shared gateway client.

Function names match the operationIds the roaming prompt uses (listRoamingLines,
findPasses, ...). The MSISDN and conversation id are added by call_gateway from
session state; they are never parameters, so the model can't see or change them.
"""

from typing import Literal, Optional

from google.adk.tools import ToolContext

from ...gateway import call_gateway
from . import mock_backend  # noqa: F401  registers dummy handlers (used when GATEWAY_URL is unset)


def _clip(value: Optional[str], limit: int = 120) -> Optional[str]:
  return value.strip()[:limit] if isinstance(value, str) else value


async def listRoamingLines(tool_context: ToolContext) -> dict:  # noqa: N802
  """Lines the caller can manage for roaming: their own line and, for a principal
  line, its share lines, with whether roaming is on and whether each can buy a
  pass. Use it when the caller means a line other than their own, or to check
  whose line a purchase or switch is for. Follow responseData.next_step.
  """
  return await call_gateway("listRoamingLines", "GET", "/roaming/lines", tool_context)


async def findPasses(  # noqa: N802
    destinations: str,
    tool_context: ToolContext,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    days: Optional[int] = None,
    line_ref: Optional[str] = None,
) -> dict:
  """Resolves the destinations the caller said (countries or cities) and finds
  roaming passes. Without a start date it lists options (for "how much is roaming
  in Japan?": answer, don't push a purchase). With a start date and an end date
  or number of days it recommends the best pass. Mention any notices before
  recommending. Follow responseData.next_step.

  Args:
    destinations: Places as the caller said them, comma-separated, e.g. "Tokyo, Seoul".
    start_date: First day of the trip, YYYY-MM-DD. Leave out if the caller hasn't said.
    end_date: Last day of the trip, YYYY-MM-DD. Prefer this over days when the caller gives dates.
    days: Trip length in days, when the caller gives a length rather than an end date.
    line_ref: The line the pass is for, from listRoamingLines. Leave out for the caller's own line.
  """
  query = {"destinations": _clip(destinations, 300), "start_date": _clip(start_date, 10),
           "end_date": _clip(end_date, 10), "days": days, "line_ref": _clip(line_ref, 40)}
  return await call_gateway("findPasses", "GET", "/roaming/passes", tool_context, query=query)


async def buyPass(  # noqa: N802
    pass_id: str,
    destinations: str,
    start_date: str,
    tool_context: ToolContext,
    line_ref: Optional[str] = None,
    confirmed: bool = False,
) -> dict:
  """Buys a roaming pass, in two steps. First call without confirmed: it returns a
  summary to read back (pass, price, start, line, and whether roaming will be
  switched on first). Only after the caller clearly says yes, call again with
  confirmed true and the same values. A start date of today buys now; a later
  date books it ahead. Follow responseData.next_step.

  Args:
    pass_id: pass_id from findPasses.
    destinations: The same destinations used in findPasses.
    start_date: The day the pass starts, YYYY-MM-DD. Today's date buys it now.
    line_ref: The line it's for, from listRoamingLines. Leave out for the caller's own line.
    confirmed: True only after the caller has agreed to the summary.
  """
  body = {"pass_id": _clip(pass_id, 40), "destinations": _clip(destinations, 300),
          "start_date": _clip(start_date, 10), "line_ref": _clip(line_ref, 40),
          "confirmed": True if confirmed is True else None}
  return await call_gateway("buyPass", "POST", "/roaming/purchases", tool_context, body=body)


async def listMyPasses(  # noqa: N802
    tool_context: ToolContext,
    when: Optional[Literal["scheduled", "active", "past", "all"]] = None,
    line_ref: Optional[str] = None,
) -> dict:
  """The caller's scheduled, active and past roaming passes (history covers the last
  60 days). Use it when the caller asks about their passes or bought one by
  mistake; pass_ref identifies a pass for helpWithWrongPass.

  Args:
    when: scheduled (not started yet: "haven't flown yet"), active (running now:
      "I'm overseas"), past (finished: "already back"), or all when unclear.
    line_ref: The line, from listRoamingLines. Leave out for the caller's own line.
  """
  query = {"when": when, "line_ref": _clip(line_ref, 40)}
  return await call_gateway("listMyPasses", "GET", "/roaming/purchases", tool_context, query=query)


async def helpWithWrongPass(  # noqa: N802
    pass_ref: str, tool_context: ToolContext, line_ref: Optional[str] = None) -> dict:
  """What can be done about a pass bought by mistake, applying Maxis's rule: a
  scheduled pass can be cancelled by the customer in the Maxis app up to 2 hours
  before it starts. The bot never cancels and never refunds. Follow
  responseData.next_step.

  Args:
    pass_ref: pass_ref from listMyPasses.
    line_ref: The line, from listRoamingLines. Leave out for the caller's own line.
  """
  body = {"pass_ref": _clip(pass_ref, 40), "line_ref": _clip(line_ref, 40)}
  return await call_gateway("helpWithWrongPass", "POST", "/roaming/wrong-pass", tool_context,
                            body=body)


async def switchRoaming(  # noqa: N802
    turn: Literal["on", "off"],
    tool_context: ToolContext,
    line_ref: Optional[str] = None,
    confirmed: bool = False,
    acknowledged_active_pass: bool = False,
) -> dict:
  """Switches international roaming on or off, in two steps like buyPass: without
  confirmed it returns what will happen; after the caller says yes to that exact
  action, call again with confirmed true. Switching off while a pass is active or
  scheduled first returns a warning; only continue if the caller still wants it
  (acknowledged_active_pass true). A change takes about 5 minutes.

  Args:
    turn: on or off.
    line_ref: The line, from listRoamingLines. Leave out for the caller's own line.
    confirmed: True only after the caller has agreed to this exact action.
    acknowledged_active_pass: True when the caller heard the warning about their
      passes and still wants roaming off.
  """
  body = {"turn": turn, "line_ref": _clip(line_ref, 40),
          "confirmed": True if confirmed is True else None,
          "acknowledged_active_pass": True if acknowledged_active_pass is True else None}
  return await call_gateway("switchRoaming", "POST", "/roaming/switch", tool_context, body=body)


ROAMING_TOOLS = [listRoamingLines, findPasses, buyPass, listMyPasses, helpWithWrongPass,
                 switchRoaming]
