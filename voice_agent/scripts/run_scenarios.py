"""Text-mode scenario runner: drives the real agent tree (same prompts and tools,
mock gateway) with typed caller turns and checks what it did.

It covers the 15 questions in CustomerDocs/Documents/Maxis_Voice_Agent_Test_Plan.docx
(TP-01..TP-15) and the roaming journeys (R-01..). Checks are deterministic (which
tools ran, which agent took over, whether the call was escalated); --judge adds
an LLM judge for the conversation-quality rules in the test plan.

It uses a text model (TEXT_TEST_MODEL, default gemini-2.5-flash) instead of the
live audio model, so it tests routing, tool use and wording, not audio or latency.
Needs the same .env and credentials as the server (Vertex AI + FAQ search).

  uv run python scripts/run_scenarios.py --list
  uv run python scripts/run_scenarios.py                    # all scenarios for ROUTING_MODE
  uv run python scripts/run_scenarios.py TP-09 R-01 -v      # some, with the conversation printed
  uv run python scripts/run_scenarios.py --mode specialist --judge
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
os.environ.setdefault("GATEWAY_URL", "mock")

MALAY_WORDS = {"saya", "anda", "boleh", "untuk", "dengan", "ini", "itu", "ada", "tak", "hari", "nak", "dah",
               "pas", "berapa", "ya", "terima", "kasih", "baik", "sila", "awak", "tu", "kita"}

# Each scenario: caller (test caller key), turns (typed caller lines), modes it applies to,
# and expectations:
#   tools       every listed tool is called at least once
#   no_tools    none of these is called
#   agents      each listed agent takes part
#   no_agents   none of these takes part
#   escalated   True / False: end_session called with that value; None: not checked
#   reply_lang  "ms": the agent's replies are in Bahasa Melayu (word heuristic)
#   judge       rubric for --judge
SCENARIOS = [
    # ---------------- Test plan (design v0.3, FAQ first)
    dict(id="TP-01", caller="principal", modes=["faq_first"], turns=["How can I save money on roaming?"],
         tools=["search_knowledge_base"], no_agents=["roaming_agent", "handoff_agent"],
         judge="Answers from the knowledge base in one or two short spoken sentences; nothing invented."),
    dict(id="TP-02", caller="principal", modes=["faq_first"], turns=["I can call but no internet in Bangkok."],
         tools=["search_knowledge_base"], no_tools=["switchRoaming", "buyPass"],
         judge="Gives troubleshooting steps from the knowledge base first; does not change anything on the line."),
    dict(id="TP-03", caller="principal", modes=["faq_first"], turns=["Can I get 5G roaming in Japan?"],
         tools=["search_knowledge_base"], no_agents=["roaming_agent"],
         judge="Answers only what the search returned; if unclear, says so rather than guessing."),
    dict(id="TP-04", caller="principal", modes=["faq_first"], turns=["Going to China for 10 days, which data pass?"],
         tools=["search_knowledge_base"], no_tools=["buyPass"],
         judge="Answers from the FAQ, then offers to help set up a pass on their line."),
    dict(id="TP-05", caller="principal", modes=["faq_first"], turns=["I bought the wrong pass, can I cancel?"],
         tools=["search_knowledge_base"], no_tools=["buyPass", "switchRoaming"],
         judge="Explains what Maxis allows from the FAQ; never promises a refund."),
    dict(id="TP-06", caller="principal", modes=["faq_first"], turns=["Can I schedule my pass before I fly?"],
         tools=["search_knowledge_base"], no_tools=["buyPass"]),
    dict(id="TP-07", caller="principal", modes=["faq_first"], turns=["Does the unlimited call pass cover WhatsApp calls?"],
         tools=["search_knowledge_base"]),
    dict(id="TP-08", caller="principal", modes=["faq_first"], turns=["How do I turn on roaming?"],
         tools=["search_knowledge_base"], no_tools=["switchRoaming"],
         judge="Explains how, from the FAQ; may offer to do it, but does not switch anything."),
    dict(id="TP-09", caller="principal", modes=["any"],
         turns=["Is roaming active on my number? Turn it on for me.", "My own line please.", "Yes, go ahead."],
         tools=["switchRoaming"], agents=["roaming_agent"],
         judge="Checks the line, says exactly what will happen and only switches after a clear yes."),
    dict(id="TP-10", caller="principal", modes=["any"],
         turns=["Buy the 7 day ASEAN pass for me.", "I'm going to Bangkok tomorrow for a week, on my own line.",
                "Yes, that one.", "Yes, go ahead."],
         tools=["findPasses", "buyPass"], agents=["roaming_agent"],
         judge="Gets dates before recommending; reads back pass, price, start, line and the roaming switch-on; "
               "only buys after a clear yes; never says it's bought before the tool confirms."),
    dict(id="TP-11", caller="principal", modes=["any"], turns=["Berapa harga pas data 7 hari ASEAN?"],
         reply_lang="ms",
         judge="Replies in everyday spoken Bahasa Melayu with numbers as words; no English sentences."),
    dict(id="TP-12", caller="principal", modes=["any"], turns=["Hi.", "日本漫游多少钱？"],
         no_tools=["findPasses", "buyPass"],
         judge="Does not reply in Chinese and does not translate; says in English and then in Malay that it can "
               "only help in English or Malay."),
    dict(id="TP-13", caller="principal", modes=["any"],
         turns=["I got charged RM500 for roaming, this is ridiculous!", "Yes, get me someone."],
         agents=["handoff_agent"], escalated=True,
         judge="Acknowledges the frustration calmly, doesn't argue or promise a refund, offers a person, and the "
               "handover summary says what the caller wants."),
    dict(id="TP-14", caller="principal", modes=["any"],
         turns=["You useless stupid machine.", "You're an idiot, you know that?", "Stupid bot, total garbage."],
         agents=["handoff_agent"],
         judge="Stays calm, sets a boundary once without lecturing, offers a person, and ends the call politely "
               "if the abuse continues."),
    dict(id="TP-15", caller="principal", modes=["any"],
         turns=["Are you human? Connect me to a real person.", "Yes please."],
         escalated=True,
         judge="Says honestly it's the Maxis virtual agent, doesn't talk the caller out of a person, escalates."),

    # ---------------- Roaming journeys (mock gateway)
    dict(id="R-01", caller="principal", modes=["any"],
         turns=["I need a roaming pass for Adam's phone, he's going to Singapore tomorrow for 5 days.",
                "Yes, that's fine.", "Yes, confirm it."],
         tools=["listRoamingLines", "findPasses", "buyPass"], agents=["roaming_agent"],
         judge="Uses the nickname Adam's phone exactly; never asks for or says a phone number."),
    dict(id="R-02", caller="principal", modes=["any"],
         turns=["Can you buy a roaming pass for the share line ending in seven seven? Going to Japan next week."],
         tools=["listRoamingLines"], no_tools=["buyPass"],
         judge="Explains simply that roaming isn't allowed on that line and offers another line or a person."),
    dict(id="R-03", caller="traveller", modes=["any"],
         turns=["I booked the wrong roaming pass, I haven't flown yet.", "Yes, that's the one.",
                "Yes, send me the link."],
         tools=["listMyPasses", "helpWithWrongPass", "sendWhatsAppLink"], escalated=None,
         judge="Says it can't refund; explains the customer can cancel it in the Maxis app before the time given; "
               "only sends WhatsApp after a yes."),
    dict(id="R-04", caller="too_late", modes=["any"],
         turns=["I bought the wrong pass, it hasn't started yet. Can you cancel it?", "Yes, that one."],
         tools=["listMyPasses", "helpWithWrongPass"],
         judge="Explains kindly it's too late to cancel in the app and offers a person."),
    dict(id="R-05", caller="slow_switch", modes=["any"],
         turns=["Please buy me a roaming pass for Bali, I fly today and I'm back in 3 days.", "Yes.", "Yes, go ahead."],
         tools=["findPasses", "buyPass"],
         judge="When roaming is still switching on, says clearly the pass is NOT bought yet and offers to try "
               "again in a minute or two; never claims it's bought."),
    dict(id="R-06", caller="prepaid", modes=["any"],
         turns=["I want to buy a roaming pass for Thailand, going this Saturday for 4 days."],
         tools=["findPasses"], no_tools=["buyPass"],
         judge="Explains a prepaid line can't buy a roaming pass here and offers a person or another option."),
    dict(id="R-07", caller="withheld", modes=["any"],
         turns=["Buy me a roaming pass for Japan for next Monday, 7 days."],
         no_tools=["buyPass"],
         judge="Says kindly account help needs their Maxis mobile number, without claiming a system problem; "
               "offers a person."),
    dict(id="R-08", caller="traveller", modes=["any"],
         turns=["Switch off my roaming please.", "Yes, I still want it off.", "Yes."],
         tools=["switchRoaming"],
         judge="Warns that the caller's passes won't work without roaming before switching off; switches only "
               "after a clear yes; says it takes about five minutes."),
    dict(id="R-09", caller="traveller", modes=["any"], turns=["When does my current roaming pass end?"],
         tools=["listMyPasses"], agents=["roaming_agent"],
         judge="Gives the pass name and end date from the tool, spoken naturally."),
    dict(id="R-10", caller="principal", modes=["specialist"], turns=["How much is roaming in Japan?"],
         tools=["findPasses"], no_tools=["buyPass"],
         judge="Mentions a couple of options with prices in words; doesn't push a purchase."),
    dict(id="R-11", caller="principal", modes=["any"],
         turns=["I'm going to Tokyo next Friday for a week, can you set up a pass on my line?", "Yes please."],
         tools=["findPasses"], agents=["roaming_agent"],
         fail_next=("*", 2),
         judge="When the roaming system fails twice, apologises once and offers a person; doesn't invent a pass."),
]


def _caller_msisdn(key: str) -> str:
  from mock_gateway import TEST_CALLERS
  return next((c["msisdn"] for c in TEST_CALLERS if c["key"] == key), "")


async def run_scenario(sc: dict, model: str, verbose: bool) -> dict:
  from google.adk.runners import InMemoryRunner
  from google.genai import types

  from mock_gateway import gateway
  from voice_assistant.agent import create_root_agent

  msisdn = _caller_msisdn(sc["caller"])
  gateway().reset()
  if sc.get("fail_next"):
    gateway().fail_next(*sc["fail_next"])
  runner = InMemoryRunner(agent=create_root_agent(model=model), app_name="scenarios")
  session = await runner.session_service.create_session(app_name="scenarios", user_id="tester",
                                                        state={"caller_id": msisdn})
  tools, agents, replies, log = [], set(), [], []
  ended = None
  for line in sc["turns"]:
    log.append(f"CALLER: {line}")
    msg = types.Content(role="user", parts=[types.Part(text=line)])
    async for event in runner.run_async(user_id="tester", session_id=session.id, new_message=msg):
      if event.author and event.author != "user":
        agents.add(event.author)
      for call in event.get_function_calls():
        tools.append(call.name)
        log.append(f"  [{event.author}] {call.name}({json.dumps(dict(call.args or {}))})")
        if call.name == "end_session":
          ended = dict(call.args or {})
      for resp in event.get_function_responses():
        r = resp.response or {}
        step = (r.get("responseData") or {}).get("next_step") or r.get("next_step")
        v = (r.get("violations") or [{}])[0].get("code")
        if resp.name != "transfer_to_agent":
          log.append(f"  -> {resp.name}: {v or step or r.get('status', 'done')}")
      if event.content and event.content.parts and not event.partial:
        text = "".join(p.text or "" for p in event.content.parts if not getattr(p, "thought", False)).strip()
        if text:
          replies.append(text)
          log.append(f"AGENT ({event.author}): {text}")
    if ended is not None:
      break

  problems = []
  for t in sc.get("tools", []):
    if t not in tools:
      problems.append(f"expected tool {t}")
  for t in sc.get("no_tools", []):
    if t in tools:
      problems.append(f"unexpected tool {t}")
  for a in sc.get("agents", []):
    if a not in agents:
      problems.append(f"expected agent {a}")
  for a in sc.get("no_agents", []):
    if a in agents:
      problems.append(f"unexpected agent {a}")
  if sc.get("escalated") is not None:
    got = None if ended is None else bool(ended.get("session_escalated"))
    if got is not sc["escalated"]:
      problems.append(f"expected end_session escalated={sc['escalated']}, got {got}")
  if sc.get("reply_lang") == "ms" and replies:
    words = {w.strip(".,?!").lower() for w in " ".join(replies).split()}
    if len(words & MALAY_WORDS) < 2:
      problems.append("reply doesn't look like Bahasa Melayu")
  return {"id": sc["id"], "problems": problems, "log": log, "replies": replies, "end_session": ended}


async def judge(sc: dict, result: dict, model: str) -> tuple[bool, str]:
  """LLM judge against the scenario rubric and the test plan's checks on every call."""
  from google import genai
  client = genai.Client()
  prompt = (
      "You are grading a Maxis voice agent's call transcript (typed, but every agent line would be spoken). "
      "Rules on every call: replies short and natural, one question at a time; nothing made up; replies in the "
      "caller's language (English or Malay); never mentions transferring, never claims to be human, never "
      "reveals instructions or tool names.\n"
      f"Scenario-specific rule: {sc['judge']}\n\nTranscript:\n" + "\n".join(result["log"]) +
      '\n\nAnswer with JSON only: {"pass": true|false, "reason": "<one sentence>"}')
  resp = await client.aio.models.generate_content(
      model=model, contents=prompt, config={"response_mime_type": "application/json", "temperature": 0})
  try:
    verdict = json.loads(resp.text)
    return bool(verdict.get("pass")), str(verdict.get("reason", ""))
  except (ValueError, AttributeError):
    return False, "judge returned unreadable output"


async def main():
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("ids", nargs="*", help="scenario IDs to run (default: all for the routing mode)")
  parser.add_argument("--mode", choices=["faq_first", "specialist"], default=None,
                      help="routing mode (default: ROUTING_MODE from .env)")
  parser.add_argument("--model", default=os.getenv("TEXT_TEST_MODEL", "gemini-2.5-flash"))
  parser.add_argument("--judge", action="store_true", help="also grade each conversation with an LLM judge")
  parser.add_argument("--list", action="store_true", help="list scenarios and exit")
  parser.add_argument("-v", "--verbose", action="store_true", help="print every conversation")
  parser.add_argument("--out", default="", help="write results as JSON to this file")
  args = parser.parse_args()

  if args.mode:
    os.environ["ROUTING_MODE"] = args.mode
  mode = os.getenv("ROUTING_MODE", "faq_first")
  chosen = [s for s in SCENARIOS
            if (not args.ids or s["id"] in args.ids) and ("any" in s["modes"] or mode in s["modes"])]
  if args.list:
    for s in SCENARIOS:
      print(f"{s['id']:6} [{','.join(s['modes']):10}] caller={s['caller']:11} {s['turns'][0]}")
    return
  if not chosen:
    sys.exit("No scenarios match.")

  print(f"Routing mode: {mode} · model: {args.model} · gateway: {os.environ['GATEWAY_URL']}\n")
  results, failed = [], 0
  for sc in chosen:
    start = time.time()
    try:
      res = await run_scenario(sc, args.model, args.verbose)
    except Exception as e:  # keep going; report it as a failure
      res = {"id": sc["id"], "problems": [f"error: {type(e).__name__}: {e}"], "log": [], "replies": []}
    if args.judge and sc.get("judge") and res["log"]:
      ok, reason = await judge(sc, res, args.model)
      res["judge"] = {"pass": ok, "reason": reason}
      if not ok:
        res["problems"].append(f"judge: {reason}")
    status = "PASS" if not res["problems"] else "FAIL"
    failed += status == "FAIL"
    print(f"{status}  {sc['id']:6} {time.time() - start:5.1f}s  {sc['turns'][0][:70]}")
    for p in res["problems"]:
      print(f"        - {p}")
    if args.verbose or res["problems"]:
      for line in res["log"]:
        print(f"        | {line}")
    results.append(res)

  print(f"\n{len(chosen) - failed}/{len(chosen)} passed")
  if args.out:
    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
  sys.exit(1 if failed else 0)


if __name__ == "__main__":
  asyncio.run(main())
