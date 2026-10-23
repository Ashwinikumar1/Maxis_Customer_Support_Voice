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

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

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
      part = types.Part(function_call=types.FunctionCall(name=name, args=args))
    else:
      part = types.Part(text=payload)
    yield LlmResponse(content=types.Content(role="model", parts=[part]))


def test_buy_a_pass_through_the_agent_tree(monkeypatch):
  monkeypatch.setenv("GATEWAY_URL", "mock")
  monkeypatch.setattr(gateway_client, "_client", None)
  gateway().reset()
  start = (datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).date() + timedelta(days=2)).isoformat()
  buy = {"pass_id": "DEMO-ASIAPACIFIC-7D", "destinations": "Tokyo", "start_date": start}
  llm = ScriptedLlm(model="scripted", script=[
      ("call", ("transfer_to_agent", {"agent_name": "roaming_agent"})),
      ("call", ("findPasses", {"destinations": "Tokyo", "start_date": start, "days": 7})),
      ("text", "The Asia Pacific Pass for seven days is fifty-eight ringgit. Shall I set it up?"),
      ("call", ("buyPass", buy)),
      ("text", "That's the pass on your own line, and I'll switch roaming on first. Go ahead?"),
      ("call", ("buyPass", {**buy, "confirmed": True})),
      ("text", "Done. You'll get an SMS when it starts."),
      ("call", ("end_session", {"session_escalated": False, "reason": "pass bought", "intent": "roaming",
                                "use_case": "buy_a_pass", "destination": "Japan"})),
      ("text", "Have a lovely trip."),
  ], seen_agents=[])
  runner = InMemoryRunner(agent=create_root_agent(model=llm), app_name="flow")

  async def call():
    session = await runner.session_service.create_session(app_name="flow", user_id="u",
                                                          state={"caller_id": "60100000001"})
    results = {}
    for line in ["I'm going to Tokyo in two days for a week, set up a pass please.", "Yes.", "Go ahead.",
                 "That's all, thanks."]:
      async for event in runner.run_async(user_id="u", session_id=session.id,
                                          new_message=types.Content(role="user", parts=[types.Part(text=line)])):
        for resp in event.get_function_responses():
          results.setdefault(resp.name, []).append(resp.response)
    final = await runner.session_service.get_session(app_name="flow", user_id="u", session_id=session.id)
    return results, final.state

  results, state = asyncio.run(call())
  assert llm.script == []
  assert llm.seen_agents[0] == "orchestrator" and set(llm.seen_agents[1:]) == {"roaming"}
  assert results["findPasses"][0]["responseData"]["next_step"] == "recommend_pass"
  assert [r["responseData"]["next_step"] for r in results["buyPass"]] == ["confirm_purchase", "purchase_done"]
  assert results["buyPass"][1]["responseData"]["roaming_switched_on"] is True
  assert state["end_session"]["agent"] == "roaming_agent"
  assert state["end_session"]["params"]["use_case"] == "buy_a_pass"
  passes = asyncio.run(gateway().list_my_passes("60100000001"))["passes"]
  assert passes[0]["name"] == "Asia Pacific Pass 7 Days"
  monkeypatch.setattr(gateway_client, "_client", None)
