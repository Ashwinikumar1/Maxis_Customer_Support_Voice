"""Measures where the greeting delay goes (no telephony, no adapter).

For each variant: time to open the Gemini Live session, then from sending the kickstart
text ("Hello") to the first audio chunk. Runs each variant --runs times.

  raw_minimal  : google-genai directly, short instruction, no tools (model + network floor)
  raw_full     : google-genai directly, the real orchestrator instruction, no tools
  adk_no_tools : ADK runner, real instruction, no FAQ tool, no sub-agents
  adk_faq      : ADK runner, real instruction + VertexAiSearchTool, no sub-agents
  adk_full     : the real agent tree (what calls use today)

Run:  ../voice_agent_server/.venv/bin/python scripts/probe_first_audio.py [--runs 2] [--only adk_full,raw_minimal]
"""

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "voice_agent_server"
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
os.environ["OTEL_TO_CLOUD"] = "false"
import certifi  # noqa: E402

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from google import genai  # noqa: E402
from google.adk.agents import Agent, LiveRequestQueue  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.adk.tools import VertexAiSearchTool  # noqa: E402
from google.genai import types  # noqa: E402

from streaming_core import default_run_config, get_datetime_info  # noqa: E402
from voice_assistant import agent as agent_mod  # noqa: E402
from voice_assistant.prompts import orchestrator_instruction  # noqa: E402

MODEL = agent_mod.LIVE_MODEL
TIMEOUT = 40.0


async def raw(instruction: str) -> tuple[float, float]:
  client = genai.Client()
  rc = default_run_config(os.getenv("VOICE_NAME", "Kore"))
  config = types.LiveConnectConfig(response_modalities=[types.Modality.AUDIO], speech_config=rc.speech_config,
                                   system_instruction=instruction)
  t0 = time.perf_counter()
  async with client.aio.live.connect(model=MODEL, config=config) as session:
    t_open = time.perf_counter()
    await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text="Hello")]))
    t_sent = time.perf_counter()
    async for msg in session.receive():
      sc = msg.server_content
      if sc and sc.model_turn and any(p.inline_data for p in sc.model_turn.parts or []):
        return t_open - t0, time.perf_counter() - t_sent
  raise RuntimeError("no audio")


async def adk(agent: Agent) -> tuple[float, float]:
  """ADK has no 'session open' event: we report kickstart->first audio, measured from run_live start."""
  sessions = InMemorySessionService()
  await sessions.create_session(app_name="probe", user_id="u", session_id="s")
  runner = Runner(app_name="probe", agent=agent, session_service=sessions)
  queue = LiveRequestQueue()
  queue.send_content(types.Content(role="user", parts=[types.Part(text="Hello")]))  # like Call: queued before connect
  t0 = time.perf_counter()
  try:
    async for event in runner.run_live(user_id="u", session_id="s", live_request_queue=queue,
                                       run_config=default_run_config(os.getenv("VOICE_NAME", "Kore"))):
      if event.content and any(p.inline_data for p in event.content.parts or []):
        return float("nan"), time.perf_counter() - t0
  finally:
    queue.close()
  raise RuntimeError("no audio")


def variants():
  today = get_datetime_info()["date"]
  instr = orchestrator_instruction(today)
  faq = lambda: [VertexAiSearchTool(data_store_id=agent_mod.FAQ_DATASTORE_ID)]  # noqa: E731
  return {
      "raw_minimal": lambda: raw("You are a phone agent. Start by saying exactly: \"Thanks for calling Maxis, how can I help?\""),
      "raw_full": lambda: raw(instr),
      "adk_no_tools": lambda: adk(Agent(name="maxis_agent", model=MODEL, instruction=instr)),
      "adk_faq": lambda: adk(Agent(name="maxis_agent", model=MODEL, instruction=instr, tools=faq())),
      "adk_full": lambda: adk(agent_mod.create_root_agent({"date": today})),
  }


async def main(a):
  print(f"model={MODEL} project={os.getenv('GOOGLE_CLOUD_PROJECT')} location={os.getenv('GOOGLE_CLOUD_LOCATION')}"
        f" instruction={len(orchestrator_instruction(get_datetime_info()['date']))} chars")
  agent_mod.warm_up_model()  # like server startup: credentials loaded before any call
  t = time.perf_counter()
  while agent_mod._creds is not None and not agent_mod._creds.valid and time.perf_counter() - t < 30:
    await asyncio.sleep(0.1)
  print(f"credentials warm-up {time.perf_counter() - t:.2f}s (server startup, not per call)")
  only = set(a.only.split(",")) if a.only else None
  for name, make in variants().items():
    if only and name not in only:
      continue
    opens, firsts = [], []
    for i in range(a.runs):
      try:
        t_open, t_first = await asyncio.wait_for(make(), TIMEOUT)
      except Exception as e:  # noqa: BLE001
        print(f"  {name} run {i + 1}: FAILED {type(e).__name__}: {str(e)[:160]}")
        continue
      opens.append(t_open); firsts.append(t_first)
      print(f"  {name} run {i + 1}: open {t_open:5.2f}s  first audio {t_first:5.2f}s", flush=True)
    if firsts:
      print(f"{name:13} median first audio {statistics.median(firsts):5.2f}s"
            + (f"  (session open {statistics.median(opens):.2f}s)" if opens and opens[0] == opens[0] else ""))


if __name__ == "__main__":
  p = argparse.ArgumentParser()
  p.add_argument("--runs", type=int, default=2)
  p.add_argument("--only", default="")
  asyncio.run(main(p.parse_args()))
