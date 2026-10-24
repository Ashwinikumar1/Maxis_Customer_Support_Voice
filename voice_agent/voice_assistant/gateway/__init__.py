"""API gateway access shared by all flows (real HTTPS or in-process mocks)."""

from .client import call_gateway
from .envelope import fail, ok

__all__ = ["call_gateway", "fail", "ok"]
