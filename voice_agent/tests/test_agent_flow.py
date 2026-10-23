"""End-to-end wiring through ADK with a scripted model (no Gemini needed).

The scripted model plays the model's side of a call: it transfers to the
roaming agent, calls the real tools (which hit the in-process mock gateway with
caller_id from session state), and ends the call. This checks the agent tree,
transfers, tool execution and end_session state; the scenario runner checks the
real model's behaviour.
"""

import asyncio
from datetime import datetime, timedelta
from typing import AsyncGenerator
from zoneinfo import ZoneInfo

import pytest
from google.adk.apps import App
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from guardrails import build_plugins, set_verification_level
from mock_gateway import gateway
from voice_assistant import gateway_client
from voice_assistant.agent import create_root_agent


class ScriptedLlm(BaseLlm):
  """Returns the next scripted reply each time any agent calls the model."""
  script: list = []
  seen_agents: list = []

  async def generate_content_async(self, llm_request, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
    instruction = str(llm_request.config.system_instruction or "")
    self.seen_agents.append("roaming" if "You are the roaming colleague" in instruction
                            else "handoff" if "You handle the moments nobody else can" in instruction
                            else "orchestrator")
    kind, payload = self.script.pop(0)
    if kind == "call":
      name, args = payload
      if args.get("confirmation_token") == "FROM_LAST_ANSWER":
        args = {**args, "confirmation_token": _last_token(llm_request, name)}
      part = types.Part(function_call=types.FunctionCall(name=name, args=args))
    else:
      part = types.Part(text=payload)
    yield LlmResponse(content=types.Content(role="model", parts=[part]))


def _last_token(llm_request, tool: str) -> str:
  """The confirmation_token in the most recent answer from `tool` (what the model would copy)."""
  for content in reversed(llm_request.contents):
    for part in content.parts or []:
      resp = part.function_response
      if resp and resp.name == tool:
        return ((resp.response or {}).get("responseData") or {}).get("confirmation_token", "")
  return ""


def _buy_script(start):
  buy = {"pass_id": "DEMO-ASIAPACIFIC-7D", "destinations": "Tokyo", "start_date": start}
  return [
      ("call", ("transfer_to_agent", {"agent_name": "roaming_agent"})),
      ("call", ("findPasses", {"destinations": "Tokyo", "start_date": start, "days": 7})),
      ("text", "The Asia Pacific Pass for seven days is fifty-eight ringgit. Shall I set it up?"),
      ("call", ("buyPass", buy)),
      ("text", "That's the pass on your own line, and I'll switch roaming on first. Go ahead?"),
      ("call", ("buyPass", {**buy, "confirmed": True, "confirmation_token": "FROM_LAST_ANSWER"})),
      ("text", "Done. You'll get an SMS when it starts."),
      ("call", ("end_session", {"session_escalated": False, "reason": "pass bought", "intent": "roaming",
                                "use_case": "buy_a_pass", "destination": "Japan"})),
      ("text", "Have a lovely trip."),
  ]


def _run_call(llm, level: int, plugins: bool = True):
  agent = create_root_agent(model=llm)
  runner = (InMemoryRunner(app=App(name="flow", root_agent=agent, plugins=build_plugins())) if plugins
            else InMemoryRunner(agent=agent, app_name="flow"))

  async def call():
    state = {"caller_id": "60100000001"}
    if level:
      set_verification_level(state, level, source="test")
    session = await runner.session_service.create_session(app_name="flow", user_id="u", state=state)
    results = {}
    for line in ["I'm going to Tokyo in two days for a week, set up a pass please.", "Yes.", "Go ahead.",
                 "That's all, thanks."]:
      async for event in runner.run_async(user_id="u", session_id=session.id,
                                          new_message=types.Content(role="user", parts=[types.Part(text=line)])):
        for resp in event.get_function_responses():
          results.setdefault(resp.name, []).append(resp.response)
    final = await runner.session_service.get_session(app_name="flow", user_id="u", session_id=session.id)
    return results, final.state

  return asyncio.run(call())


@pytest.fixture
def mock_gw(monkeypatch):
  monkeypatch.setenv("GATEWAY_URL", "mock")
  monkeypatch.setenv("WRITE_TOOLS_ENABLED", "true")
  monkeypatch.setattr(gateway_client, "_client", None)
  gateway().reset()
  yield
  monkeypatch.setattr(gateway_client, "_client", None)


def _start():
  return (datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).date() + timedelta(days=2)).isoformat()


def test_write_refused_without_level_2(mock_gw):
  """A caller ID alone (level 1) can look up passes but not buy (guardrails AP-04)."""
  llm = ScriptedLlm(model="scripted", script=_buy_script(_start()), seen_agents=[])
  results, _ = _run_call(llm, level=1)
  assert results["findPasses"][0]["responseData"]["next_step"] == "recommend_pass"
  refused = results["buyPass"][1]
  assert (refused["status"], refused["reason"]) == ("refused", "verification_required")
  assert asyncio.run(gateway().list_my_passes("60100000001"))["passes"] == []


def test_write_refused_when_kill_switch_off(mock_gw, monkeypatch):
  monkeypatch.setenv("WRITE_TOOLS_ENABLED", "false")
  llm = ScriptedLlm(model="scripted", script=_buy_script(_start()), seen_agents=[])
  results, _ = _run_call(llm, level=2)
  assert results["buyPass"][1]["reason"] == "writes_disabled"


def test_confirmation_token_must_match(mock_gw):
  script = _buy_script(_start())
  script[5] = ("call", ("buyPass", {"pass_id": "DEMO-ASIAPACIFIC-7D", "destinations": "Tokyo",
                                    "start_date": _start(), "confirmed": True, "confirmation_token": "made-up"}))
  llm = ScriptedLlm(model="scripted", script=script, seen_agents=[])
  results, _ = _run_call(llm, level=2)
  assert results["buyPass"][1]["reason"] == "confirmation_missing"


def test_buy_a_pass_through_the_agent_tree(mock_gw):
  llm = ScriptedLlm(model="scripted", script=_buy_script(_start()), seen_agents=[])
  results, state = _run_call(llm, level=2)
  assert llm.script == []
  assert llm.seen_agents[0] == "orchestrator" and set(llm.seen_agents[1:]) == {"roaming"}
  assert results["findPasses"][0]["responseData"]["next_step"] == "recommend_pass"
  assert [r["responseData"]["next_step"] for r in results["buyPass"]] == ["confirm_purchase", "purchase_done"]
  assert results["buyPass"][1]["responseData"]["roaming_switched_on"] is True
  assert state["end_session"]["agent"] == "roaming_agent"
  assert state["end_session"]["params"]["use_case"] == "buy_a_pass"
  assert "confirmation_token" in results["buyPass"][0]["responseData"]
  passes = asyncio.run(gateway().list_my_passes("60100000001"))["passes"]
  assert passes[0]["name"] == "Asia Pacific Pass 7 Days"
