"""Shared fixtures: a frozen, movable clock (Malaysia time) and a fresh mock gateway."""

import asyncio
from datetime import datetime, timedelta

import pytest

from mock_gateway import service
from mock_gateway.service import MYT, MockGateway, call


class Clock:
  def __init__(self, start: datetime):
    self.now = start

  def advance(self, **kwargs):
    self.now += timedelta(**kwargs)

  def day(self, offset: int) -> str:
    return (self.now.date() + timedelta(days=offset)).isoformat()


@pytest.fixture
def clock(monkeypatch):
  c = Clock(datetime(2026, 10, 21, 10, 0, tzinfo=MYT))  # a Wednesday, 10 am
  monkeypatch.setattr(service, "_now", lambda: c.now)

  async def _no_sleep(_seconds):
    return None
  monkeypatch.setattr(service.asyncio, "sleep", _no_sleep)
  return c


@pytest.fixture
def gw(clock):
  return MockGateway()


@pytest.fixture
def run(gw):
  """run("find_passes", caller_id=..., ...) -> response envelope."""
  def _run(operation, **kwargs):
    return asyncio.run(call(gw, operation, **kwargs))
  return _run


PRINCIPAL = "60100000001"
TRAVELLER = "60100000002"
TOO_LATE = "60100000003"
SLOW = "60100000004"
PREPAID = "60100000005"
NOT_MAXIS = "60190000009"
