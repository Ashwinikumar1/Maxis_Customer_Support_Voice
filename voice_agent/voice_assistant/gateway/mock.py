"""In-process stand-in for the API gateway, used when GATEWAY_URL is unset.

Each flow registers handlers by OpenAPI operationId (see flows/*/mock_backend.py).
A handler receives (ctx, query, body) and returns the gateway response envelope.
Per-call state (purchases, switches, ...) lives in session_store(ctx.conversation_id):
in memory, bounded, and gone on restart - it is demo data only.
"""

from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from .envelope import fail

_MAX_STORES = 500  # most recent calls kept; older demo state is dropped


@dataclass(frozen=True)
class MockContext:
  msisdn: str           # which demo account answers
  conversation_id: str  # per-call state key


Handler = Callable[[MockContext, dict, dict], dict]
_HANDLERS: dict[str, Handler] = {}
_STORES: "OrderedDict[str, dict]" = OrderedDict()


class BadRequest(ValueError):
  """Raised by a handler for invalid input; returned as an INVALID_REQUEST violation."""


def register(operation_id: str) -> Callable[[Handler], Handler]:
  """Decorator: registers the mock handler for an OpenAPI operationId."""
  def decorator(fn: Handler) -> Handler:
    _HANDLERS[operation_id] = fn
    return fn
  return decorator


def session_store(key: str) -> dict:
  """Mutable per-call state for mock handlers (LRU-bounded)."""
  store = _STORES.get(key)
  if store is None:
    store = _STORES[key] = {}
    while len(_STORES) > _MAX_STORES:
      _STORES.popitem(last=False)
  else:
    _STORES.move_to_end(key)
  return store


def dispatch(operation_id: str, ctx: MockContext, query: dict, body: dict) -> dict:
  handler = _HANDLERS.get(operation_id)
  if handler is None:
    return fail("BACKEND_UNAVAILABLE", f"No mock registered for {operation_id}.",
                "apologise_and_offer_agent")
  try:
    return handler(ctx, query, body)
  except BadRequest as e:
    return fail("INVALID_REQUEST", str(e), "fix_the_request")
