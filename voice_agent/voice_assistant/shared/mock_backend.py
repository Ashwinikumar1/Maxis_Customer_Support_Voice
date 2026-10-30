"""Dummy gateway answers for the shared tools (used only when GATEWAY_URL is unset).

TODO: align operation paths and payloads with the real gateway contracts for
WhatsApp messaging and agent handoff (they aren't in the roaming spec).
"""

from ..gateway.envelope import ok
from ..gateway.mock import BadRequest, register


@register("sendWhatsAppLink")
def _send_whatsapp_link(ctx, query, body):
  if body.get("kind") not in ("insurance", "manage_passes"):
    raise BadRequest("kind must be insurance or manage_passes.")
  return ok({"sent": True, "kind": body["kind"], "next_step": "confirm_sent"})


@register("escalateToAgent")
def _escalate_to_agent(ctx, query, body):
  if not body.get("reason") or not body.get("customer_request"):
    raise BadRequest("reason and customer_request are required.")
  return ok({"queued": True, "wait_minutes": 3, "next_step": "tell_caller_connecting"})
