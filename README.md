# Maxis Customer Support Voice Agent

A real-time voice agent for Maxis built with **Google ADK** and **Gemini Live** (Vertex AI). It
answers in English or Malay, uses the Maxis FAQ, helps with roaming on the caller's own line, and
takes phone calls through **Genesys**. Runs as one container on **Cloud Run**.

| Area | Status |
|---|---|
| ADK agent: FAQ, roaming flow, Maxis gateway | ✅ Working (staging gateway; write actions disabled until approved). Greeting ~2 s |
| Escalation to a human agent | ⚠️ Placeholder code only, **not working yet**: deferred until the Genesys integration works |
| Genesys telephony (`/ces` bridge) | 🟢 Works end to end locally with the real `ces-genesys-adapter` code ([voice_agent_client/adapter_test](voice_agent_client/adapter_test/README.md)); [rollout plan](Documents/Genesys_Adapter_Rollout.md) waiting on Maxis |
| Cloud Run in Maxis project | ⏳ Waiting on service account, IAM and approval |

Progress tracker: [`Documents/Maxis_Voice_Agent_Tracker.xlsx`](Documents/Maxis_Voice_Agent_Tracker.xlsx).

---

## 1. The ADK voice agent

```
test client (laptop) ─┐
                      ├─/ces─▶ server.py (Cloud Run) ─▶ Call ─▶ ADK run_live() ─▶ Gemini Live
Genesys ─▶ adapter ───┘                                 maxis_agent ── VertexAiSearchTool ─▶ Maxis FAQ data store
                                                        └─ roaming_agent ── tools ─▶ Maxis agent gateway (staging)
```

**Server and client are separate.** The [server](voice_agent_server/README.md) runs on Cloud Run and has
only one way in: `/ces`, the protocol the Genesys adapter speaks. The [client](voice_agent_client/README.md)
runs on your laptop for testing and calls `/ces` the same way Genesys will (same messages, 8 kHz
phone audio, same token). So moving from testing to real phone calls changes nothing on the server.

**Agents and tools**

| Agent | Does | Tools |
|---|---|---|
| `maxis_agent` (orchestrator) | Answers Maxis questions from the FAQ first; hands "my line" requests to a specialist | `VertexAiSearchTool` (FAQ) |
| `roaming_agent` | Roaming on the caller's line | `listRoamingLines`, `findPasses`, `listMyPasses` (live); `buyPass`, `switchRoaming`, `helpWithWrongPass`, `sendWhatsAppLink` (disabled until approved); `escalateToAgent` (placeholder) |

**How it behaves**
- English and Malay only. Other languages get a fixed bilingual refusal.
- Says a short line before each tool call; keyboard clicks cover tool and transfer waits.
- The caller's number (MSISDN) comes from the channel, never from the model. Gateway calls send
  `X-Caller-Id` and `X-Conversation-Id` with a Google ID token.
- Tool errors become a polite "internal error" reply (`@safe_tool`).
- New flows (fibre, billing…) copy `flows/roaming/` and are added to `FLOWS`.

**Run locally** (two terminals; both `.env` files share the same `CES_BRIDGE_TOKEN`)

```bash
gcloud auth login --update-adc
cd voice_agent_server && cp .env.example .env    # project, FAQ data store, gateway URL, CES_BRIDGE_TOKEN
uv sync && uv run python server.py   # ws://127.0.0.1:8080/ces/...

cd voice_agent_client && cp .env.example .env    # SERVER_URL + the same CES_BRIDGE_TOKEN
uv sync && uv run python client.py   # http://127.0.0.1:8090 → "Start talking"
```

To test the Cloud Run server, deploy with `voice_agent_server/deploy.sh` and point the client's `SERVER_URL`
at `wss://<service>/ces/test`.

---

## 2. Genesys telephony: same adapter as GECX

> [!IMPORTANT]
> Phone calls use the **same `ces-genesys-adapter` that GECX (Gemini Enterprise for CX / CX Agent
> Studio) uses**, already running for Maxis. No new Genesys integration is built. The adapter's
> backend URL points to our **custom ADK code** instead of GECX. Our `/ces` bridge answers the
> same CES protocol ([`channels/ces_bridge.py`](voice_agent_server/channels/ces_bridge.py)).

```
GECX today :  Genesys ─▶ ces-genesys-adapter ─▶ GECX / CES agent
This repo  :  Genesys ─▶ ces-genesys-adapter ─▶ /ces bridge ─▶ ADK agent (Gemini Live)
                         └──── same adapter, same Genesys setup ────┘
Testing    :  client (laptop) ────────────────▶ /ces bridge   (same protocol, same audio)
```

The adapter keeps doing all the Genesys work: auth, audio pacing, barge-in, DTMF, and
`disconnect` with output variables. It needs **one line** changed (CES URL from an env var). It is
deployed as a second service, so the GECX bot is untouched.

**Params**

| Direction | How |
|---|---|
| Genesys → agent | Architect input variables → ADK session state (`channel_vars`); `msisdn = Call.Ani` → caller number |
| Agent → Genesys | Call outcome → output variables (`outcome`, `handoff_reason`, `summary`, `use_case`…) for Architect routing |

**Pending (after permission):** adapter change + token secret → deploy the server →
Architect flow and staging number → end-to-end call → production hardening.

Full protocol, output variables and rollout steps:
[`voice_agent_server/README.md`](voice_agent_server/README.md#genesys-telephony-bridge-ces).

---

## Repository layout

```text
Documents/          design, test plan, Genesys notes, progress tracker, architecture images
voice_agent_server/  deployed to Cloud Run (no UI)
├── server.py           /ces (Genesys adapter + test client) and /healthz, auth, telemetry
├── voice_assistant/    the ADK agent: agent.py, prompts.py, flows/roaming/, shared/, gateway/
├── streaming_core/     channel-agnostic call: Call, Live session, audio, bridge sound, transcripts
├── channels/           ces_bridge.py: the CES protocol (Genesys adapter, test client)
├── scripts/            test_ces_bridge.py (offline test)
└── deploy.sh, Dockerfile, .env.example
voice_agent_client/  local testing only (never deployed)
├── client.py           browser UI + proxy that calls /ces like Genesys
├── ces_adapter_sim.py  WAV-driven caller (scripted tests)
├── ces.py, audio.py    CES messages, MULAW/PCM conversion
└── static/             browser UI
```

More: [`voice_agent_server/README.md`](voice_agent_server/README.md) has configuration, testing, deploy, security,
troubleshooting and the production checklist; [`voice_agent_client/README.md`](voice_agent_client/README.md) covers testing.
