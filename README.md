# Maxis Customer Support Voice Agent

A real-time voice agent for Maxis built with **Google ADK** and **Gemini Live** (Vertex AI). It
answers in English or Malay, uses the Maxis FAQ, helps with roaming on the caller's own line, and
takes phone calls through **Genesys**. Runs as one container on **Cloud Run**.

| Area | Status |
|---|---|
| ADK agent: FAQ, roaming flow, Maxis gateway | ✅ Working (staging gateway; write actions disabled until approved) |
| Escalation to a human agent | ⚠️ Placeholder code only, **not working yet**: needs changes + testing |
| Genesys telephony (`/ces` bridge) | 🟡 Built + offline-tested; deploy waiting on permission |
| Cloud Run in Maxis project | ⏳ Waiting on service account, IAM and approval |

Progress tracker: [`Documents/Maxis_Voice_Agent_Tracker.xlsx`](Documents/Maxis_Voice_Agent_Tracker.xlsx).

---

## 1. The ADK voice agent

```
caller audio ─▶ server.py ─▶ Call ─▶ ADK run_live() ─▶ Gemini Live
                                       maxis_agent ── VertexAiSearchTool ─▶ Maxis FAQ data store
                                       └─ roaming_agent ── tools ─▶ Maxis agent gateway (staging)
```

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

**Run locally**

```bash
gcloud auth login --update-adc
cd voice_agent && cp .env.example .env    # set project, FAQ data store, gateway URL
uv sync && uv run python server.py        # http://127.0.0.1:8080 → "Start talking"
```

---

## 2. Genesys telephony: same adapter as GECX

> [!IMPORTANT]
> Phone calls use the **same `ces-genesys-adapter` that GECX (Gemini Enterprise for CX / CX Agent
> Studio) uses**, already running for Maxis. No new Genesys integration is built. The adapter's
> backend URL points to our **custom ADK code** instead of GECX. Our `/ces` bridge answers the
> same CES protocol ([`channels/ces_bridge.py`](voice_agent/channels/ces_bridge.py)).

```
GECX today :  Genesys ─▶ ces-genesys-adapter ─▶ GECX / CES agent
This repo  :  Genesys ─▶ ces-genesys-adapter ─▶ /ces bridge ─▶ ADK agent (Gemini Live)
                         └──── same adapter, same Genesys setup ────┘
```

The adapter keeps doing all the Genesys work: auth, audio pacing, barge-in, DTMF, and
`disconnect` with output variables. It needs **one line** changed (CES URL from an env var). It is
deployed as a second service, so the GECX bot is untouched.

**Params**

| Direction | How |
|---|---|
| Genesys → agent | Architect input variables → ADK session state (`channel_vars`); `msisdn = Call.Ani` → caller number |
| Agent → Genesys | Call outcome → output variables (`outcome`, `handoff_reason`, `summary`, `use_case`…) for Architect routing |

**Pending (after permission):** adapter change + token secret → deploy with the bridge on →
Architect flow and staging number → end-to-end call → production hardening.

Full protocol, output variables and rollout steps:
[`voice_agent/README.md`](voice_agent/README.md#genesys-telephony-bridge-ces).

---

## Repository layout

```text
Documents/          design, test plan, Genesys notes, progress tracker, diagrams
voice_agent/
├── server.py           HTTP UI + /ws (browser) + /ces (Genesys), auth checks, telemetry
├── voice_assistant/    the ADK agent: agent.py, prompts.py, flows/roaming/, shared/, gateway/
├── streaming_core/     channel-agnostic call: Call, Live session, audio, bridge sound, transcripts
├── channels/           ces_bridge.py: Genesys via the ces-genesys-adapter
├── scripts/            test_ces_bridge.py (offline test), ces_adapter_sim.py (adapter stand-in)
├── static/             browser test UI
└── deploy.sh, Dockerfile, .env.example
```

More: [`voice_agent/README.md`](voice_agent/README.md) has configuration, testing, deploy,
security, troubleshooting and the production checklist.
