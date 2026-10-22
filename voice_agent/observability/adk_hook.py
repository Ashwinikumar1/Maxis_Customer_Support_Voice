"""Attaches our span processor to the providers ADK creates.

server.py calls `google.adk.telemetry.setup.maybe_set_otel_providers(...)`
inside `_setup_otel()` (only when OTEL_TO_CLOUD=true) and imports it at call
time, so wrapping the module attribute before server.py runs is enough to add
SessionSpanProcessor next to ADK's Cloud Trace exporter. Providers that were
not set up stay untouched (local runs keep OTel off, as today).
"""

import logging

logger = logging.getLogger("voice_agent.observability")

_installed = False


def install() -> None:
  global _installed
  if _installed:
    return
  from google.adk.telemetry import setup as adk_setup

  from .spans import SessionSpanProcessor

  original = adk_setup.maybe_set_otel_providers

  def maybe_set_otel_providers(otel_hooks_to_setup=None, otel_resource=None):
    hooks = list(otel_hooks_to_setup or [])
    if any(h.span_processors for h in hooks):  # only when tracing is actually exported
      hooks.append(adk_setup.OTelHooks(span_processors=[SessionSpanProcessor()]))
    original(hooks, otel_resource=otel_resource)
    logger.info("Observability add-on: session span processor attached")

  maybe_set_otel_providers.__wrapped__ = original
  adk_setup.maybe_set_otel_providers = maybe_set_otel_providers
  _installed = True
