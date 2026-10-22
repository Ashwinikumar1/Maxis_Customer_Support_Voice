"""Structured, trace-correlated logging for Cloud Logging (gaps #1 and #2).

- CloudJsonFormatter: one JSON object per line on stdout, using the special
  fields Cloud Logging lifts out of jsonPayload (severity, message, time,
  logging.googleapis.com/trace, /spanId, /trace_sampled, /sourceLocation), so
  container logs nest under their trace in Logs Explorer and Trace Explorer.
  Exceptions put the traceback in `message` with the ReportedErrorEvent type
  so Error Reporting picks them up as one entry.
- TraceTextFormatter: the existing local format plus a short trace suffix.
- SpanErrorHandler: any ERROR log record emitted inside a recording span marks
  that span ERROR (and records the exception). The server's existing
  `logger.exception("Session ... failed")` runs inside the `voice_session`
  span, so failed calls now show as failed traces without editing server.py.
"""

import json
import logging
import sys
from datetime import datetime, timezone

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from . import settings

_ERROR_EVENT_TYPE = "type.googleapis.com/google.devtools.clouderrorreporting.v1beta1.ReportedErrorEvent"
_SEVERITY = {"WARN": "WARNING", "FATAL": "CRITICAL"}
_NOISY_LOGGERS = ("websockets",)  # logs "connection rejected (200 OK)" for every static file


def _span_fields():
  """(trace_id_hex, span_id_hex, sampled, session_id) for the current span, or None."""
  span = trace.get_current_span()
  ctx = span.get_span_context()
  if not ctx.is_valid:
    return None
  attrs = getattr(span, "attributes", None) or {}
  session_id = attrs.get("session.id") or attrs.get("gen_ai.conversation.id")
  return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}", ctx.trace_flags.sampled, session_id


class CloudJsonFormatter(logging.Formatter):

  def format(self, record: logging.LogRecord) -> str:
    message = record.getMessage()
    entry = {
        "severity": _SEVERITY.get(record.levelname, record.levelname),
        "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
        "logger": record.name,
        "logging.googleapis.com/sourceLocation": {
            "file": record.pathname, "line": str(record.lineno), "function": record.funcName},
    }
    fields = _span_fields()
    if fields:
      trace_id, span_id, sampled, session_id = fields
      project = settings.project_id()
      entry["logging.googleapis.com/trace"] = f"projects/{project}/traces/{trace_id}" if project else trace_id
      entry["logging.googleapis.com/spanId"] = span_id
      entry["logging.googleapis.com/trace_sampled"] = sampled
      if session_id:
        entry["session_id"] = session_id
    if record.exc_info:
      # Traceback inside `message` keeps it ONE entry and lets Error Reporting group it.
      message = f"{message}\n{self.formatException(record.exc_info)}"
      entry["@type"] = _ERROR_EVENT_TYPE
    entry["message"] = message
    return json.dumps(entry, ensure_ascii=False, default=str)


class TraceTextFormatter(logging.Formatter):

  def __init__(self):
    super().__init__("%(asctime)s - %(levelname)s - %(message)s")

  def format(self, record: logging.LogRecord) -> str:
    text = super().format(record)
    fields = _span_fields()
    return f"{text} [trace={fields[0][:8]} span={fields[1][:8]}]" if fields else text


class SpanErrorHandler(logging.Handler):
  """Marks the active span ERROR for every ERROR+ record logged inside it."""

  def __init__(self):
    super().__init__(level=logging.ERROR)

  def emit(self, record: logging.LogRecord) -> None:
    try:
      span = trace.get_current_span()
      if not span.is_recording():
        return
      if record.exc_info and isinstance(record.exc_info[1], BaseException):
        span.record_exception(record.exc_info[1], attributes={"log.logger": record.name})
      span.set_status(Status(StatusCode.ERROR, record.getMessage()[:256]))
    except Exception:  # telemetry must never break the app
      pass


def configure_logging() -> None:
  """Installs the root handlers. Must run BEFORE streaming_core is imported:
  its logging.basicConfig() is then a no-op because the root logger already
  has handlers."""
  root = logging.getLogger()
  for handler in list(root.handlers):
    if getattr(handler, "_obs_installed", False):
      root.removeHandler(handler)

  out = logging.StreamHandler(sys.stdout)
  out.setFormatter(CloudJsonFormatter() if settings.log_format() == "json" else TraceTextFormatter())
  errors = SpanErrorHandler()
  for handler in (out, errors):
    handler._obs_installed = True
    root.addHandler(handler)
  root.setLevel(settings.log_level())
  # Python warnings (e.g. ADK's "[EXPERIMENTAL] feature ... is enabled") otherwise go to
  # stderr as raw multi-line text; route them through logging so they are JSON too.
  logging.captureWarnings(True)

  for name in _NOISY_LOGGERS:
    logging.getLogger(name).setLevel(max(logging.WARNING, root.level))
