"""Traces and logs the Vertex AI Search call behind the FAQ tool (gap #3).

`search_knowledge_base` swallows every exception and ADK records the tool as
successful. Its blocking helper `_search` is looked up in the module's globals
on every call, so wrapping that attribute gives each search:

- a CLIENT span `vertex_ai_search.search` (ERROR status + exception on failure,
  HTTP status code, result count), nested under ADK's execute_tool span;
- an ERROR log line with the exception type and traceback;
- `voice.faq_search.duration` (Histogram, ms) by outcome.

The caller's query is PII, so only its length is recorded.
"""

import functools
import logging
import time

from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind

logger = logging.getLogger("voice_agent.faq_search")
_tracer = trace.get_tracer("maxis_voice_agent.observability")
_meter = metrics.get_meter("maxis_voice_agent.observability")
_latency = _meter.create_histogram(
    "voice.faq_search.duration", unit="ms", description="Vertex AI Search latency for the FAQ tool, by outcome")


def _status_code(exc: BaseException) -> int | None:
  response = getattr(exc, "response", None)
  return getattr(response, "status_code", None)


def wrap(search_fn, engine_id: str):
  if getattr(search_fn, "_obs_wrapped", False) is True:
    return search_fn

  @functools.wraps(search_fn)
  def traced_search(query: str):
    start = time.perf_counter()
    outcome = "ok"
    with _tracer.start_as_current_span("vertex_ai_search.search", kind=SpanKind.CLIENT, attributes={
        "server.address": "discoveryengine.googleapis.com",
        "vertex_ai_search.engine_id": engine_id,
        "vertex_ai_search.query_length": len(query or ""),
    }) as span:
      try:
        results = search_fn(query)
        span.set_attribute("vertex_ai_search.result_count", len(results))
        if not results:
          outcome = "empty"
        return results
      except Exception as exc:
        outcome = type(exc).__name__
        code = _status_code(exc)
        if code is not None:
          span.set_attribute("http.response.status_code", code)
        logger.exception("FAQ search failed (%s%s)", outcome, f", HTTP {code}" if code else "")
        raise
      finally:
        _latency.record((time.perf_counter() - start) * 1000, {"outcome": outcome})

  traced_search._obs_wrapped = True
  return traced_search


def install() -> None:
  from voice_assistant import faq_search_tool as tool

  tool._search = wrap(tool._search, tool._ENGINE)
