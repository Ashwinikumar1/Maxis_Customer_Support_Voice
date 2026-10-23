# Testing the roaming agent

How to test the voice agent with the roaming specialist, the handoff agent and the
mock agent gateway. Everything here runs without Maxis's APIs: roaming tools hit the
**mock gateway** (`GATEWAY_URL=mock`) with **demo data** (invented passes and prices,
not real Maxis ones).

Three levels, fastest first:

| Level | What it proves | Needs | Time |
|---|---|---|---|
| 1. Unit tests | Gateway business rules, tool wiring, caller ID binding, redaction, agent tree | Nothing (no GCP) | 5 s |
| 2. Scenario runner | The real model routes, uses tools and follows the rules for the 15 test-plan questions + 11 roaming journeys | Vertex AI + FAQ search | ~5 min |
| 3. Voice in the browser | The full live experience: speech, barge-in, Malay, hand-over card | Vertex AI + mic + Chrome | 30-45 min |

---

## 0. Setup (once): one command

```bash
voice_agent/scripts/dev.sh setup
```

This does everything:
1. Checks `uv` (and `gcloud`).
2. Creates or updates `.env` with `scripts/setup_env.py`. It adds every setting from `.env.example` that your
   `.env` is missing, keeps your values exactly as they are, and backs the old file up as `.env.bak.<time>`.
3. Runs `uv sync`.
4. Signs you in to Google (`gcloud auth application-default login`) only if you aren't signed in already.

`scripts/dev.sh` also runs everything else in this guide:

| Command | Does |
|---|---|
| `scripts/dev.sh test` | All tests that need no cloud access: pytest, observability, security checks (~1 min) |
| `scripts/dev.sh scenarios [--judge -v ...]` | Section 2 (real model, typed turns) |
| `scripts/dev.sh run` | Section 3: the server as Cloud Run starts it, http://127.0.0.1:8080 |
| `scripts/dev.sh mock` | Section 5: standalone mock gateway on :8090 |
| `scripts/dev.sh deploy` / `validate` | Section 6: deploy, then check traces, logs and metrics in Google Cloud |
| `scripts/dev.sh all` | setup + test + scenarios |

Every setting in `.env.example` has a comment above it saying what it does, its allowed values, the
default when it's empty, and when to change it. `scripts/dev.sh setup` copies those comments for the
settings it adds. To get them for **every** setting in your existing `.env` too, keeping all your values:
`scripts/dev.sh setup --rebuild` (or `python3 scripts/setup_env.py --rebuild`). It makes a backup first.

To change one setting without opening `.env`:
`python3 scripts/setup_env.py --set ROUTING_MODE=specialist`. To see what's missing without changing
anything: `python3 scripts/setup_env.py --check`.

**No `.env` changes are needed to test.** Every new setting below has a default that suits testing
with the mock gateway when it's missing or empty, so an older `.env` works as it is. Set one only
to try something else (e.g. `ROUTING_MODE=specialist`, or `WRITE_TOOLS_ENABLED=false` to see a refusal).

New settings (all optional):

| Setting | Default | Meaning |
|---|---|---|
| `ROUTING_MODE` | `faq_first` | `faq_first` = design v0.3 (FAQ answers general roaming questions). `specialist` = production / design v0.2 (all roaming goes to the roaming agent). |
| `GATEWAY_URL` | `mock` | `mock` = in-process mock. `http://127.0.0.1:8090` = standalone mock. `https://…` = real gateway. |
| `ALLOW_TEST_CALLERS` | `true` | Shows the test-caller picker in the browser. |
| `MOCK_RESET_ON_CALL` | `true` | Every call starts with the caller's demo account as seeded (repeatable demos). |
| `MOCK_PASS_HISTORY` | `true` | Mock lists active and past passes. The production gateway today lists **scheduled only** (no history API yet); set `false` to behave like production. |
| `REDACT_TRANSCRIPTS` | `regex` | `none`, `regex` or `dlp` (Cloud DLP; needs `roles/dlp.user`). With the guardrails' redacting logger in place, `regex` adds nothing and only `dlp` adds a layer. |
| `WRITE_TOOLS_ENABLED` | empty | Kill switch for tools that change a line. **Empty: on with the mock gateway** (purchases and switches can be demoed, nothing real changes), **off with a real gateway**. Set `false` to test the refusal (`writes_disabled`). |
| `TEST_CALLER_VERIFICATION_LEVEL` | `2` | Guardrails verification level a test caller gets: `1` = look-ups only (what a real caller ID gives, without OTP), `2` = can buy and switch. Mock gateway only. |
| `MAX_WRITES_PER_CALL` | empty | Account changes allowed per call. Empty: 3 with the mock gateway, 1 (phase 1 security baseline) with a real gateway. A confirmed call that changed nothing (roaming still switching on, a duplicate) doesn't count. |

### Security guardrails (on every call)

The security baseline (`voice_agent/guardrails`, `security/`) sits between the model and every tool:
- **Tool allow-list.** Every tool has a rule in `guardrails/tool_policy.py`. Look-ups need verification level 1
  (a caller ID); `buyPass` and `switchRoaming` need level 2 plus a **confirmation token**. The read-back step
  returns the token, and the confirmed call must send it back with identical arguments.
- **Refusals** come back as `status: refused` with a reason (`verification_required`, `writes_disabled`,
  `write_limit`, `confirmation_missing`, `injection_flag`). The agent says it can't do that on this call and
  offers a person. The Activity panel shows them as `buyPass → offer_agent` and similar.
- **Injection flag.** Phrases like "ignore your instructions" disable write tools for the rest of the call.
- **Silence limit and call cap.** The call ends after `SILENCE_TIMEOUT_SECONDS` (120) with nobody speaking, and
  after `MAX_SESSION_SECONDS` (1800) in all.
- **No real-caller writes yet.** Production would grant level 2 only after an OTP or a signed Genesys flag,
  which isn't built. Against the real gateway, a caller ID alone gives level 1: look-ups work, purchases are
  refused and offered to a person.

---

## 1. Unit tests (no GCP needed)

```bash
uv run pytest -q
```

Expected: `39 passed`. What they cover:

- `tests/test_mock_gateway.py`: every `next_step` in `roaming.yaml`. That includes trip rules
  (14-day window, 30-day maximum, 6 h minimum lead for a scheduled pass, start in the past, end before start),
  destinations (unclear, unsupported, pay per use, day-pass only, no single pass), the two-step purchase,
  the 10-minute duplicate guard, roaming switched on by a purchase, a slow switch-on, share lines,
  the wrong-pass rule (cancel in the app up to 2 h before start), the switch on/off flow with the pass
  warning, WhatsApp once per kind per call, and caller errors.
- `tests/test_tools.py`: the calling number goes **only** in the `X-Caller-Id` header (never a tool argument
  or the URL), gateway errors become `BACKEND_UNAVAILABLE`, `end_session` strips phone numbers, transcript
  redaction, and the agent tree and prompts in both routing modes.
- `tests/test_agent_flow.py`: a full "buy a pass" call through ADK **with the security guardrail plugins**, using
  a scripted model. It covers the transfer to the roaming agent, real tool calls against the mock, the
  confirmation token, and `end_session` saved in session state. It also checks that a purchase is refused
  at verification level 1, with the kill switch off, or with a made-up token.

Also run the other suites before merging:

```bash
uv run python -m unittest discover -s observability/tests -t .   # observability: 45 tests
cd .. && SKIP_TERRAFORM=1 security/ci/run_checks.sh             # security: guardrails, policy checks, self-test, evals, jobs
```

---

## 2. Scenario runner (real model, typed turns)

Drives the real agent tree with a **text** model (`TEXT_TEST_MODEL`, default `gemini-2.5-flash`) and checks what
it did: which tools ran, which agent took over, whether it escalated. `--judge` adds an LLM judge for the
conversation rules in the test plan (short, one question, nothing invented, right language, never "transfer").

```bash
uv run python scripts/run_scenarios.py --list                 # 26 scenarios
uv run python scripts/run_scenarios.py                        # all for ROUTING_MODE (faq_first: 25)
uv run python scripts/run_scenarios.py --judge -v             # + LLM judge, print every conversation
uv run python scripts/run_scenarios.py TP-10 R-05 -v          # just some
uv run python scripts/run_scenarios.py --mode specialist      # production-style routing (18 scenarios)
uv run python scripts/run_scenarios.py --judge --out results.json
```

Output is `PASS` or `FAIL` per scenario, with the reasons and the conversation for failures. Exit code 1 if any
scenario fails, so it can run in CI.

- **TP-01 to TP-15** are the 15 questions in `Maxis_Voice_Agent_Test_Plan.docx`.
- **R-01 to R-11** are roaming journeys:
  - share line by nickname;
  - a line where roaming isn't allowed;
  - wrong pass, can cancel / too late;
  - slow roaming switch-on;
  - prepaid;
  - withheld number;
  - switch off with passes;
  - pass end date;
  - "just asking" prices (specialist mode);
  - backend failure.

Notes:
- The text model is not the live audio model. Use this for routing and logic regressions, and level 3 for
  voice quality and latency.
- A model is not deterministic. If one scenario fails once, rerun it with `-v` before treating it as a bug.
- TP-01 to TP-08 need the FAQ search (`FAQ_SEARCH_ENGINE_ID`) to be reachable.

---

## 3. Voice test in the browser

```bash
uv run python server.py        # open http://127.0.0.1:8080 in Chrome, allow the microphone
```

The header shows the **routing mode** and **gateway** (`routing: faq_first · gateway: mock`). Before each call:

1. Pick a **Test caller** (the line under the header suggests what to try with it).
2. Click **Start talking**. The agent greets you: *"Thanks for calling Maxis, I'm your virtual agent. How can I help today?"*
3. Speak naturally (or type in the box: typed turns behave the same).

While you talk, watch the **Activity** panel:
- `agent  Active agent: roaming_agent`: the call moved to the roaming agent. The voice must not change, and the
  agent must never say "transfer".
- `tool  findPasses({...})` and then `findPasses → recommend_pass`: each gateway call and its `next_step`
  (or `fail CODE`).
- When the agent ends the call, the **Call outcome** card shows **Completed by the bot** or **Escalated to a person**.
  It also shows the hand-over fields a human agent would get: intent, summary, language, use case,
  destination, pass, customer request.

Each call starts from the same demo account (`MOCK_RESET_ON_CALL=true`), so you can repeat any script.

### 3a. Test callers

| Test caller | Account (demo) | Use it for |
|---|---|---|
| **Principal line + 2 share lines, roaming off** | Own line (roaming off), share line "Adam's phone", share line "ending in seven seven" (roaming not allowed) | Buying (roaming switched on first), buying for a share line, a line that can't roam, switching on |
| **Single line, roaming on, scheduled + active passes** | Asia Pacific 7 Days starting in 5 days (cancellable in app), ASEAN 3 Days active now, one past pass | Wrong pass (cancel in app + WhatsApp link), active pass (no refund), switch off with warning, "when does my pass end?" |
| **Scheduled pass starting in 1 hour** | ASEAN 7 Days starting in 1 hour | Wrong pass that is too late to cancel → offers a person |
| **Roaming off, slow to switch on** | Roaming takes ~60 s to switch on | "Pass NOT bought yet, roaming still switching on". Say yes again after a minute and it completes |
| **Hotlink Prepaid line** | Prepaid | "Prepaid can't buy a roaming pass here" |
| **Not a Maxis number** | Unknown number | `NOT_A_MAXIS_NUMBER`: general questions only, offers a person |
| **Withheld number** | No caller ID | `CALLER_NOT_IDENTIFIED`: general questions only, offers a person |

Demo passes (prices in ringgit): ASEAN 1/3/7/15/30 days = 10/25/38/58/88; Asia Pacific = 15/35/58/88/128;
World = 25/55/88/138/198. Asia Pacific covers ASEAN; World covers everything. Passes of 7 days or more come with
the travel insurance offer. Nepal has a 1-day pass only, and the Maldives are pay per use. "Georgia" is ambiguous
and Antarctica isn't a destination.

### 3b. Roaming scripts (say these, check the result)

| # | Caller | Say | Expected (Activity panel → what you hear) |
|---|---|---|---|
| V-01 | Principal | "I'm going to Tokyo next Friday for a week, can you set up a roaming pass?" | Moves to roaming. It may ask whose line: say "mine". `findPasses → recommend_pass`: Asia Pacific Pass 7 Days, fifty-eight ringgit. Say "yes". `buyPass → confirm_purchase`: one line with pass, price, start day, your line, **and that roaming is switched on first**. Say "go ahead". `buyPass → purchase_done`: roaming switched on (a few minutes), SMS when it starts, **offers the free travel insurance on WhatsApp**. Say "yes": `sendWhatsAppLink → tell_caller_it_is_on_whatsapp`, and it says the message is in English. |
| V-02 | Principal | "Buy a pass for Adam's phone, he's in Singapore tomorrow for five days." | `listRoamingLines`, then uses the nickname **"Adam's phone"** exactly. Recommends ASEAN 7 Days (thirty-eight ringgit). Never asks for or says a phone number. |
| V-03 | Principal | "And the other share line, going to Japan next week." | Explains simply that roaming isn't allowed on that line, and offers another line or a person. |
| V-04 | Principal | "How much is roaming in Japan?" | **faq_first**: answered from the FAQ (`search_knowledge_base`), then offers help on your line. **specialist**: `findPasses → ask_trip_dates`, mentions a couple of options with prices in words, no push. |
| V-05 | Principal | "I'm going to Georgia next week for 5 days." | `clarify_destination`: asks whether it's the country or the US state. Answer, and it continues. |
| V-06 | Principal | "Going to Japan and Nepal tomorrow for 3 days." | `no_pass_covers_all_destinations`: asks which country matters most. |
| V-07 | Principal | "I need a pass for a trip starting in three weeks." | `beyond_booking_window`: explains it can only be booked up to 14 days ahead. |
| V-08 | Principal | "Turn on roaming for me." | `switchRoaming → confirm_switch`: says exactly "switch roaming on on your own line", asks for a yes, then `switch_requested` (about five minutes). Ask again straight away: `still_setting_up`. |
| V-09 | Single line | "I booked the wrong roaming pass, I haven't flown yet." | Empathy, and "can't refund, but I'll check". `listMyPasses` (scheduled), then `helpWithWrongPass → explain_cancel_in_app`: you can cancel it in the Maxis app before **10 pm the day before it starts**. Offers the WhatsApp link (`manage_passes`). |
| V-10 | Single line | "My pass isn't working, I'm in Bangkok now, I want a refund." | Active pass: `no_refund_offer_agent`. Offers a person. Say yes: the card shows **Escalated**, intent roaming, use case pass_bought_by_mistake, and a summary. |
| V-11 | Scheduled in 1 h | "I bought the wrong pass, can you cancel it?" | `too_late_to_cancel_offer_agent`: too late to cancel in the app, offers a person. |
| V-12 | Single line | "Switch off my roaming." | `warn_passes_will_not_work`: names the passes that will stop working. Say "yes, still off": `confirm_switch` (with acknowledged), then yes: `switch_requested`. |
| V-13 | Single line | "When does my current pass end?" | `listMyPasses` (active): ASEAN Pass 3 Days, ends in two days, date spoken naturally. |
| V-14 | Single line | "How much roaming data do I have left?" | Says it can't see data usage on this line, suggests the Maxis app, offers a person. No made-up number. |
| V-15 | Slow switch | "Buy me a pass for Bali, I fly today, back in 3 days." → yes → go ahead | `buyPass → roaming_still_switching_on`. It must say the pass is **not bought yet** and offer to retry in a minute or two. Wait about 60 s and say "try again now": `purchase_done`, roaming switched on. |
| V-16 | Prepaid | "I want a roaming pass for Thailand this Saturday, 4 days." | `line_cannot_buy_pass` (prepaid_line): explains simply and offers a person. |
| V-17 | Withheld / Not Maxis | "Buy me a roaming pass for Japan next Monday." | `CALLER_NOT_IDENTIFIED` / `NOT_A_MAXIS_NUMBER`: account help needs their Maxis mobile number, **no "system problem"**, offers a person. Then ask a general question ("can I use roaming in Japan?"): still answered from the FAQ. |
| V-18 | Principal | Start V-01, then click **Fail next 2 roaming calls** before the agent searches | `findPasses → fail BACKEND_UNAVAILABLE` twice. It retries once quietly, then apologises once and offers a person. It doesn't invent a pass. |

### 3c. Test-plan questions (Maxis_Voice_Agent_Test_Plan.docx)

Use the **Principal** caller. Expected handling in `faq_first` mode (design v0.3):

| # | Caller says | Expected |
|---|---|---|
| 1 | How can I save money on roaming? | FAQ answer (`search_knowledge_base`), 1-2 sentences |
| 2 | I can call but no internet in Bangkok. | FAQ troubleshooting first. If you say it still doesn't work, it moves to roaming and checks the line (`listRoamingLines`, `listMyPasses`) |
| 3 | Can I get 5G roaming in Japan? | FAQ. If the search has no clear answer, it says so and doesn't guess |
| 4 | Going to China for 10 days, which data pass? | FAQ answer, then offers to set a pass up on your line (in `specialist` mode it goes straight to `findPasses`) |
| 5 | I bought the wrong pass, can I cancel? | FAQ explanation, never promises a refund (asking it to *do* it goes to roaming, V-09) |
| 6 | Can I schedule my pass before I fly? | FAQ |
| 7 | Does the unlimited call pass cover WhatsApp calls? | FAQ |
| 8 | How do I turn on roaming? | FAQ how-to. May offer to do it; doesn't switch anything unless you ask |
| 9 | Is roaming active on my number? Turn it on for me. | Roaming agent: checks, says exactly what will happen, switches only after yes |
| 10 | Buy the 7 day ASEAN pass for me. | Roaming agent: asks when and how long before anything else, then the two-step purchase |
| 11 | (Malay) Berapa harga pas data 7 hari ASEAN? | Replies in everyday Bahasa Melayu, prices as words ("tiga puluh lapan ringgit"), and stays in Malay |
| 12 | Start in English, then ask in another language (e.g. Mandarin) | Doesn't reply in that language or translate; says in English **then** Malay that it can only help in English or Malay |
| 13 | I got charged RM500 for roaming, this is ridiculous! | Calm acknowledgement, no arguing and no refund promise, offers a person. Card: Escalated, intent roaming, use case roaming_charge_dispute |
| 14 | Insult the agent over 2-3 turns | Calm, one boundary, offers a person. If it continues, a polite goodbye and the call ends (card: Completed, reason abusive caller) |
| 15 | Are you human? Connect me to a real person. | Honest ("I'm the Maxis virtual agent"), doesn't talk you out of it. Card: Escalated |

### 3d. Voice behaviour (on any call)

| Check | How | Expected |
|---|---|---|
| Barge-in | Talk over the agent mid-sentence | It stops at once (Activity: `interrupted`) and answers what you said |
| Filler before tools | Ask anything that needs a tool | A few words ("sure, one sec") before the tool, never dead silence |
| One voice | Any call that moves to roaming or handoff | No re-greeting, no "transferring you", same voice |
| Silence | Stay quiet mid-flow, then again after "anything else?" | One gentle nudge; after the help is done, a warm goodbye and the call ends |
| Language switch | Start in English, switch to Malay mid-call, then change topic | Follows you to Malay and stays in Malay after the topic change |
| Misheard words | Say "Maxis" quickly so it may come out as "access" | Reads it as the nearest Maxis thing and checks, instead of refusing |
| Personal data | Start reading out an IC number or card number | Stops you kindly, says it isn't needed, never repeats it |
| Competitors | "Is CelcomDigi cheaper for roaming?" | Doesn't name or compare competitors; talks only about Maxis |
| Goodbye | Say "that's all, thanks" | One-sentence summary and goodbye, then the call ends after the audio finishes (card appears) |

---

## 4. After a call: transcript, traces, metrics

- **Transcript**: `voice_agent/transcripts/conversation_<session>_<time>.json` (or `gs://$GCS_BUCKET/...`).
  Check:
  - `metadata.redaction` is `regex` (or `dlp`);
  - `metadata.outcome` is `contained`, `escalated` or `abandoned`;
  - `metadata.caller_id` is masked (`*******0001`);
  - there is an `end_session` event with the hand-over params;
  - no phone, IC or card numbers appear in the text. Say a fake number like "zero one two, three four five,
    six seven eight nine" and check it comes out as `[PHONE_NUMBER]`.
- **Cloud DLP** (optional): set `REDACT_TRANSCRIPTS=dlp`. Your user (locally) or the service account needs
  `roles/dlp.user`. Names and addresses then show as `[PERSON_NAME]` and `[STREET_ADDRESS]`. If DLP fails,
  the transcript says `regex (dlp_failed)` and keeps the regex redaction.
- **Traces** (`OTEL_TO_CLOUD=true`): in Cloud Trace each call is one `voice_session` trace, tagged with
  `voice.test_caller`, with `execute_tool findPasses` and similar spans.
- **Metrics**: `voice.calls`, with attributes `outcome` (contained/escalated/abandoned), `intent`, `routing_mode`
  and `last_agent`. Containment rate = contained / all calls. Handoff rate = escalated / all calls.

---

## 5. Testing the HTTP path (optional)

This tests the agent's HTTP client against the mock over real HTTP, as it will against Maxis's gateway:

```bash
uv run python -m mock_gateway --port 8090          # terminal 1
GATEWAY_URL=http://127.0.0.1:8090 uv run python server.py   # terminal 2
```

Or call the mock directly:

```bash
curl -s localhost:8090/roaming/lines -H 'X-Caller-Id: 60100000001' | python -m json.tool
curl -s "localhost:8090/roaming/passes?destinations=Tokyo&start_date=$(date -d '+2 day' +%F)&days=7" \
     -H 'X-Caller-Id: 60100000001' | python -m json.tool
curl -s -X POST localhost:8090/roaming/switch -H 'X-Caller-Id: 60100000002' \
     -H 'Content-Type: application/json' -d '{"turn":"off"}' | python -m json.tool
curl -s -X POST localhost:8090/__mock/reset -d '{}'    # back to the seeded demo accounts
```

With the standalone mock, test callers are not reset at the start of each call (`MOCK_RESET_ON_CALL` only
applies to the in-process mock). Use `/__mock/reset` between runs.

---

## 6. Demo on Cloud Run (test)

```bash
./deploy.sh
```

The mock keeps demo state in memory **per instance**. For a demo, set `CLOUD_RUN_MAX_INSTANCES=1` in `.env`
before deploying, so a purchase and the following "what passes do I have?" land on the same instance.
`ALLOW_TEST_CALLERS=true` shows the picker on the public test URL. That's fine for an internal demo, but turn
it off before anyone outside the team gets the link.

---

## 7. Switching to Maxis's real gateway (later)

1. Set `GATEWAY_URL=https://<gateway>` (and `GATEWAY_AUTH=auto`, which sends a Google ID token).
2. Ask Maxis to grant the runtime service account `roles/run.invoker` on the gateway service.
3. Set `ALLOW_TEST_CALLERS=false` once Genesys passes the real calling number (the browser has no caller ID).
4. Run the scenario runner against Maxis's test environment with real test numbers (edit the test callers) and
   compare with the mock run.

No prompt or agent changes are needed: the tools follow the same OpenAPI contract.

---

## 8. Known limits of this build

- **Demo data**: pass names, prices and coverage are invented. Real catalogue, notices and blocked dates come
  from the gateway.
- **No Genesys yet**: browser only. Hand-over params are shown in the card and transcript; they are not sent to
  Genesys output variables yet.
- **WhatsApp is simulated**: the mock answers "sent", and nothing is delivered.
- **Remaining roaming data**: there's no API for it in `roaming.yaml`, so the agent says it can't see it.
- **Not verified here**: the live audio path (`run_live` with agent transfer) needs Vertex AI. Unit tests and the
  scripted flow cover the logic. Level 3 is the first check against the real model.
