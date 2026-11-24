"""The gateway's standard response envelope (status / responseData / violations)."""

from datetime import datetime, timezone


def _timestamp() -> str:
  return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ok(data: dict) -> dict:
  """A success envelope around responseData."""
  return {"status": "success", "responseData": data, "timeStamp": _timestamp()}


def fail(code: str, message: str, action: str) -> dict:
  """A fail envelope. The model follows violations[0].action (see the OpenAPI Violation schema)."""
  return {
      "status": "fail",
      "message": message,
      "violations": [{"code": code, "message": message, "action": action}],
      "timeStamp": _timestamp(),
  }
