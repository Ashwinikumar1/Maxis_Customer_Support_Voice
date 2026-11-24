"""Maxis agent gateway access shared by all flows (HTTPS, Google ID token + X-Caller-Id)."""

from .client import call_gateway
from .envelope import fail, ok
from .safe_tool import INTERNAL_ERROR_MESSAGE, internal_error, safe_tool

__all__ = ["call_gateway", "fail", "ok", "safe_tool", "internal_error",
           "INTERNAL_ERROR_MESSAGE"]
