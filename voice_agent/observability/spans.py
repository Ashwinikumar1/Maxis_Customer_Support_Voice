"""Session ID on every span of a call (gap #7).

SessionSpanProcessor is registered on ADK's TracerProvider (see adk_hook.py).
It copies `session.id` from a parent span to every child span that lacks it,
and sets the standard `gen_ai.conversation.id`, so ADK's agent and tool spans
can be filtered by call in Trace Explorer.

[Observability #13] The call metrics (voice.sessions.active,
voice.session.duration, voice.sessions) used to be computed here from the
`voice_session` span. They moved to server.py: with trace sampling on, the
OpenTelemetry SDK runs no span processor for unsampled calls, so metrics built
here would have undercounted by the sampling rate. Copying attributes onto
spans is still right here - unsampled spans are never exported anyway.
"""

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

SESSION_SPAN = "voice_session"
_SESSION_ID = "session.id"
_CONVERSATION_ID = "gen_ai.conversation.id"


class SessionSpanProcessor(SpanProcessor):

  def on_start(self, span: Span, parent_context=None) -> None:
    try:
      if span.name == SESSION_SPAN:
        return
      parent = trace.get_current_span(parent_context)
      parent_attrs = getattr(parent, "attributes", None) or {}
      session_id = parent_attrs.get(_SESSION_ID) or parent_attrs.get(_CONVERSATION_ID)
      own = span.attributes or {}
      if session_id:
        if _SESSION_ID not in own:
          span.set_attribute(_SESSION_ID, session_id)
        if _CONVERSATION_ID not in own:
          span.set_attribute(_CONVERSATION_ID, session_id)
    except Exception:
      pass

  def on_end(self, span: ReadableSpan) -> None:
    pass

  def shutdown(self) -> None:
    pass

  def force_flush(self, timeout_millis: int = 30000) -> bool:
    return True
