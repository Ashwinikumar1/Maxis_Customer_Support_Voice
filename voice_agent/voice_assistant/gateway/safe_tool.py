"""One try/except for every tool: on any error the caller hears a short apology."""

import functools
import logging

from .envelope import fail

logger = logging.getLogger("voice_agent.tools")

INTERNAL_ERROR_MESSAGE = "I'm facing an internal error, please try again later."


def internal_error() -> dict:
  return fail("INTERNAL_ERROR", INTERNAL_ERROR_MESSAGE, "say_internal_error_try_later")


def safe_tool(fn):
  """Wraps an async tool so it never raises; errors become internal_error()."""
  @functools.wraps(fn)  # keeps name, docstring and signature for ADK
  async def wrapper(*args, **kwargs):
    try:
      return await fn(*args, **kwargs)
    except Exception:
      logger.exception("Tool %s failed", fn.__name__)
      return internal_error()
  return wrapper
