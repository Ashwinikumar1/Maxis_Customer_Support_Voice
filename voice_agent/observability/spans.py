"""Session-level telemetry from the spans the app already creates (gaps #7 and #9).

SessionSpanProcessor is registered on ADK's TracerProvider (see adk_hook.py).
It reads the existing `voice_session` span, so nothing in server.py changes:

- voice.sessions.active   (UpDownCounter)  calls in progress on this instance
- voice.session.duration  (Histogram, s)   call length, by outcome
- voice.sessions          (Counter)        finished calls, by outcome (ok / error)
                                           and close_reason (from server.py)

It also copies `session.id` from a parent span to every child span that lacks
it, and sets the standard `gen_ai.conversation.id`, so ADK's agent and tool
spans can be filtered by call in Trace Explorer.
"""

from opentelemetry import metrics, trace
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import StatusCode

SESSION_SPAN = "voice_session"
_SESSION_ID = "session.id"
_CONVERSATION_ID = "gen_ai.conversation.id"

_meter = metrics.get_meter("maxis_voice_agent.observability")
_active = _meter.create_up_down_counter(
    "voice.sessions.active", description="Voice calls in progress on this instance")
_duration = _meter.create_histogram(
    "voice.session.duration", unit="s", description="Voice call duration, by outcome")
_finished = _meter.create_counter("voice.sessions", description="Finished voice calls, by outcome")


class SessionSpanProcessor(SpanProcessor):

  def on_start(self, span: Span, parent_context=None) -> None:
    try:
      if span.name == SESSION_SPAN:
        _active.add(1)
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
    try:
      if span.name != SESSION_SPAN:
        return
      _active.add(-1)
      outcome = "error" if span.status.status_code == StatusCode.ERROR else "ok"
      if span.start_time and span.end_time:
        _duration.record((span.end_time - span.start_time) / 1e9, {"outcome": outcome})
      close_reason = (span.attributes or {}).get("voice.close_reason", "unknown")
      _finished.add(1, {"outcome": outcome, "close_reason": close_reason})
    except Exception:
      pass

  def shutdown(self) -> None:
    pass

  def force_flush(self, timeout_millis: int = 30000) -> bool:
    return True
