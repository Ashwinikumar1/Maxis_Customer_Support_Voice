"""Tool layer: caller ID binding, gateway client error mapping, end_session, redaction, agent tree."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from streaming_core.redaction import redact_transcript, regex_redact
from voice_assistant import gateway_client, prompts, roaming_tools
from voice_assistant.session_tools import end_session


def ctx(caller_id="60100000001", agent="roaming_agent"):
  return SimpleNamespace(state={"caller_id": caller_id}, session=SimpleNamespace(id="session_abc"),
                         agent_name=agent)


@pytest.fixture
def captured(monkeypatch):
  """Replaces the gateway's HTTP client with one that records requests."""
  seen = []

  def handler(request: httpx.Request):
    seen.append(request)
    return httpx.Response(200, json={"status": "success", "responseData": {"next_step": "ok"},
                                     "timeStamp": "t"})

  monkeypatch.setattr(gateway_client, "_client",
                      httpx.AsyncClient(base_url="http://gw.test", transport=httpx.MockTransport(handler)))
  monkeypatch.setenv("GATEWAY_URL", "http://gw.test")
  return seen


def test_caller_id_goes_only_in_the_header(captured):
  asyncio.run(roaming_tools.findPasses("Tokyo, Seoul", ctx(), start_date="2026-10-23", days=7))
  req = captured[0]
  assert req.method == "GET" and req.url.path == "/roaming/passes"
  assert req.headers["X-Caller-Id"] == "60100000001"
  assert req.headers["X-Conversation-Id"] == "session_abc"
  assert "60100000001" not in str(req.url)
  assert dict(req.url.params) == {"destinations": "Tokyo, Seoul", "start_date": "2026-10-23", "days": "7"}


def test_post_body_and_withheld_caller(captured):
  asyncio.run(roaming_tools.buyPass("DEMO-ASEAN-7D", "Bali", "2026-10-23", ctx(caller_id=""), confirmed=True))
  req = captured[0]
  assert "X-Caller-Id" not in req.headers
  assert json.loads(req.content) == {"pass_id": "DEMO-ASEAN-7D", "destinations": "Bali",
                                     "start_date": "2026-10-23", "confirmed": True}


def test_no_tool_accepts_a_phone_number():
  import inspect
  for tool in roaming_tools.ROAMING_TOOLS:
    params = set(inspect.signature(tool).parameters)
    assert not params & {"caller_id", "msisdn", "phone", "phone_number"}, tool.__name__


def test_gateway_errors_become_fail_envelopes(monkeypatch):
  def boom(request):
    raise httpx.ConnectError("down")
  monkeypatch.setattr(gateway_client, "_client",
                      httpx.AsyncClient(base_url="http://gw.test", transport=httpx.MockTransport(boom)))
  resp = asyncio.run(roaming_tools.listRoamingLines(ctx()))
  assert resp["status"] == "fail"
  assert resp["violations"][0]["code"] == "BACKEND_UNAVAILABLE"

  monkeypatch.setattr(gateway_client, "_client", httpx.AsyncClient(
      base_url="http://gw.test", transport=httpx.MockTransport(lambda r: httpx.Response(503, text="x"))))
  assert asyncio.run(roaming_tools.listRoamingLines(ctx()))["violations"][0]["action"] == \
      "apologise_and_offer_agent"


def test_in_process_mock_end_to_end(monkeypatch):
  monkeypatch.setenv("GATEWAY_URL", "mock")
  monkeypatch.setattr(gateway_client, "_client", None)
  resp = asyncio.run(roaming_tools.listRoamingLines(ctx()))
  assert resp["responseData"]["next_step"] == "confirm_which_line_if_unclear"
  resp = asyncio.run(roaming_tools.listMyPasses(ctx(caller_id="")))
  assert resp["violations"][0]["code"] == "CALLER_NOT_IDENTIFIED"
  monkeypatch.setattr(gateway_client, "_client", None)


def test_end_session_records_clean_handover():
  c = ctx()
  out = end_session(True, "caller asked for a person", c, intent="roaming", language="ms",
                    summary="Wanted a refund for a pass. Call me on 012-345 6789.", use_case="pass_bought_by_mistake",
                    destination="Japan", pass_name="Asia Pacific Pass 7 Days")
  assert out["next_step"] == "call_ending"
  saved = c.state["end_session"]
  assert saved["session_escalated"] is True and saved["agent"] == "roaming_agent"
  assert saved["params"]["language"] == "ms"
  assert "012" not in saved["params"]["summary"]
  assert saved["params"]["pass"] == "Asia Pacific Pass 7 Days"
  bad = end_session(False, "done", ctx(), intent="weird", use_case="nope")
  assert bad["status"] == "success"


def test_regex_redaction_keeps_dates_and_prices():
  assert regex_redact("IC 900101-14-5678, phone 012-345 6789") == "IC [IC_NUMBER], phone [PHONE_NUMBER]"
  assert regex_redact("starts 2026-10-23 for fifty-eight ringgit") == "starts 2026-10-23 for fifty-eight ringgit"


def test_transcript_redaction(monkeypatch):
  monkeypatch.setenv("REDACT_TRANSCRIPTS", "regex")
  data = {"metadata": {}, "conversation": [
      {"speaker": "User", "text": "my number is 0123456789", "timestamp": "t"},
      {"speaker": "System", "event": "tool_call", "args": {"summary": "call +60 12 345 6789"}, "timestamp": "t"}]}
  redact_transcript(data)
  assert data["metadata"]["redaction"] == "regex"
  assert "[PHONE_NUMBER]" in data["conversation"][0]["text"]
  assert data["conversation"][1]["args"]["summary"] == "call [PHONE_NUMBER]"
  assert data["conversation"][1]["timestamp"] == "t"


@pytest.mark.parametrize("mode", ["faq_first", "specialist"])
def test_agent_tree_and_prompts(monkeypatch, mode):
  monkeypatch.setenv("ROUTING_MODE", mode)
  from voice_assistant.agent import create_root_agent
  root = create_root_agent()
  assert [a.name for a in root.sub_agents] == ["roaming_agent", "handoff_agent"]
  for agent in (root, *root.sub_agents):
    assert "{" not in agent.instruction and "__" not in agent.instruction  # no ADK placeholders left
  assert prompts.GREETING in root.instruction
  assert ("FAQ first" in root.instruction) == (mode == "faq_first")
  roaming_tool_names = {t.__name__ for t in root.sub_agents[0].tools}
  assert {"findPasses", "buyPass", "switchRoaming", "end_session"} <= roaming_tool_names
  assert "end_session" in {t.__name__ for t in root.sub_agents[1].tools}
