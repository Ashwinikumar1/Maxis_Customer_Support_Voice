"""Test harness: the real server with a scripted model, for end-to-end roaming calls.

Everything is real except Gemini: server.py call handling, the ADK agent tree with the
guardrail plugins, the roaming tools, the mock gateway, the observability hooks and the
transcripts. The scripted model plays the model's side of each call (tool calls and
replies), and after every caller turn it also sends the kind of event Gemini Live sends
for a spoken reply (an audio chunk, the reply's transcription, a running token total and
turn_complete), so model_turn spans, time to first audio and token usage are exercised too.

Spans go to OBS_TEST_SPANS_FILE, metrics to OBS_TEST_METRICS_FILE (via _harness), logs to
stdout. ROAMING_CALLS picks the calls, in order (comma-separated keys of CALLS).
"""

import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from observability.tests import _harness
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

START = (datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).date() + timedelta(days=2)).isoformat()
BUY = {"pass_id": "DEMO-ASIAPACIFIC-7D", "destinations": "Tokyo", "start_date": START}

CALLS = {
    # buy a pass: contained
    "buy": dict(fail=None, turns=["Buy a Tokyo pass", "Yes", "Go ahead", "Thanks, bye"], script=[
        ("call", ("transfer_to_agent", {"agent_name": "roaming_agent"})),
        ("call", ("listRoamingLines", {})),
        ("call", ("findPasses", {"destinations": "Tokyo", "start_date": START, "days": 7})),
        ("text", "The Asia Pacific pass is fifty-eight ringgit. Shall I set it up?"),
        ("call", ("buyPass", BUY)),
        ("text", "Your own line, roaming switched on first. Go ahead?"),
        ("call", ("buyPass", {**BUY, "confirmed": True, "confirmation_token": "FROM_LAST_ANSWER"})),
        ("text", "Done."),
        ("call", ("end_session", {"session_escalated": False, "reason": "pass bought", "intent": "roaming",
                                  "use_case": "buy_a_pass", "destination": "Japan"})),
        ("text", "Have a lovely trip."),
    ]),
    # the gateway fails twice: escalated
    "down": dict(fail=("findPasses", 2), turns=["Buy a Tokyo pass", "Yes, a person please"], script=[
        ("call", ("transfer_to_agent", {"agent_name": "roaming_agent"})),
        ("call", ("findPasses", {"destinations": "Tokyo", "start_date": START, "days": 7})),
        ("call", ("findPasses", {"destinations": "Tokyo", "start_date": START, "days": 7})),
        ("text", "Sorry, I'm having trouble. Shall I get you to a person?"),
        ("call", ("end_session", {"session_escalated": True, "reason": "gateway down", "intent": "roaming",
                                  "use_case": "buy_a_pass", "summary": "Wanted a Tokyo pass; system down."})),
        ("text", "Getting you to someone now."),
    ]),
    # run with TEST_CALLER_VERIFICATION_LEVEL=1: the purchase is refused by the tool policy
    "refused": dict(fail=None, turns=["Buy a Tokyo pass", "Go ahead", "Bye"], script=[
        ("call", ("transfer_to_agent", {"agent_name": "roaming_agent"})),
        ("call", ("buyPass", BUY)),
        ("call", ("buyPass", {**BUY, "confirmed": True, "confirmation_token": "FROM_LAST_ANSWER"})),
        ("text", "Sorry, I can't make that change on this call."),
        ("call", ("end_session", {"session_escalated": False, "reason": "refused", "intent": "roaming"})),
        ("text", "Bye."),
    ]),
}

_USAGE = types.GenerateContentResponseUsageMetadata  # one name, used below for every report


def _last_token(llm_request, tool):
  for content in reversed(llm_request.contents):
    for part in content.parts or []:
      r = part.function_response
      if r and r.name == tool:
        return ((r.response or {}).get("responseData") or {}).get("confirmation_token", "")
  return ""


class ScriptedLlm(BaseLlm):
  """Returns the next scripted step each time any agent calls the model."""
  script: list = []

  async def generate_content_async(self, llm_request, stream=False):
    kind, payload = self.script.pop(0) if self.script else ("text", "...")
    if kind == "call":
      name, args = payload
      if args.get("confirmation_token") == "FROM_LAST_ANSWER":
        args = {**args, "confirmation_token": _last_token(llm_request, name)}
      part = types.Part(function_call=types.FunctionCall(name=name, args=args))
    else:
      part = types.Part(text=payload)
    yield LlmResponse(content=types.Content(role="model", parts=[part]),
                      usage_metadata=_USAGE(prompt_token_count=100, candidates_token_count=10, total_token_count=110))


def _spoken_reply(agent: str, n: int) -> Event:
  """What Gemini Live sends for a spoken reply: audio, its transcription, a running token total."""
  return Event(
      author=agent,
      content=types.Content(role="model", parts=[
          types.Part(inline_data=types.Blob(data=bytes(4800), mime_type="audio/pcm;rate=24000"))]),
      output_transcription=types.Transcription(text=f"reply {n}", finished=True),
      usage_metadata=_USAGE(prompt_token_count=200 * n, candidates_token_count=40 * n, total_token_count=240 * n),
      turn_complete=True,
  )


def _install():
  _harness._install_with_file_exporter()
  from mock_gateway import gateway
  from streaming_core import live_session
  from voice_assistant import agent as agent_mod

  queue = os.environ["ROAMING_CALLS"].split(",")
  current = {}
  real_create = agent_mod.create_root_agent

  def create(ctx=None, model=None):
    name = queue.pop(0)
    current["call"] = CALLS[name]
    return real_create(ctx, model=ScriptedLlm(model="scripted", script=list(CALLS[name]["script"])))

  agent_mod.create_root_agent = create

  async def run(self, emit):
    call = current["call"]
    if call["fail"]:
      gateway().fail_next(*call["fail"])
    agent = None
    for n, line in enumerate(call["turns"], start=1):
      self.turn.start(trigger="user_text")  # as when the caller has finished speaking
      msg = types.Content(role="user", parts=[types.Part(text=line)])
      async for event in self.runner.run_async(user_id=self.user_id, session_id=self.session_id, new_message=msg):
        # Token reports come only from the live stream (the spoken reply below), as with Gemini Live,
        # where usage is one running total per call; the scripted text replies' usage feeds ADK only.
        await self._handle(event.model_copy(update={"usage_metadata": None}), emit, self.turn, agent)
        if event.author and event.author != "user":
          agent = event.author
      await self._handle(_spoken_reply(agent or "maxis_agent", n), emit, self.turn, agent)
    await asyncio.sleep(30)  # the server ends the call after end_session

  live_session.LiveVoiceSession.run = run


_harness.launcher.install = _install

if __name__ == "__main__":
  _harness.launcher.main()
