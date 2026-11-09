# Maxis voice agent: what we need from Maxis

_7 Nov 2026. Project `mxs-maxisapp-prd`. Our voice agent runs on Cloud Run as `maxis-voice-agent`
(us-central1)._

**Goal:** a real staging call: Genesys → **new copy of the Genesys adapter** → `maxis-voice-agent`.
The existing GECX adapter and route stay as they are.

**Blocking items:** 1, 2 (with 3), 6, 7, 11.

## A. Access and permissions

| # | Request | Why |
|---|---|---|
| 1 | Turn **off** the Cloud Run invoker IAM check on `maxis-voice-agent` (the same org policy exception the existing adapter has) | Without it, Cloud Run blocks the adapter before the call reaches our agent. Our own shared token check stays on. |
| 2 | Grant `roles/run.invoker` on `voicebot-cx-studio-agent-gateway` to our service's service account | Account and roaming questions fail today: only the GECX service agent may call the gateway |
| 3 | A **dedicated service account** for our agent (e.g. `maxis-voice-agent@`) instead of the shared default compute SA. Roles: Vertex AI User, Discovery Engine viewer on the FAQ data store, Secret Accessor on `voice-agent-ces-token`, object creator on the transcript bucket | If #2 went to the shared compute SA, every service running as that SA could call the PROD gateway |
| 4 | Confirm **PROD or staging gateway** for the test phase | The deployed agent points at PROD today |

## B. Copy of the Genesys adapter

| # | Request |
|---|---|
| 5 | The adapter source or version you deployed, so we can apply our patch (Google's latest code also works) |
| 6 | Deploy the copy, e.g. `ces-genesys-adapter-adk-stg`, with our one-line patch (`CES_WS_URL`) and these env vars:<br>`CES_WS_URL=wss://maxis-voice-agent-657431854992.us-central1.run.app/ces/`<br>`AUTH_TOKEN_SECRET_PATH=projects/mxs-maxisapp-prd/secrets/voice-agent-ces-token` |
| 7 | The **same org policy exception** for the copy (invoker IAM check off, so Genesys can connect) |
| 8 | Its own service account, with Secret Accessor on `voice-agent-ces-token` and on its Genesys secrets |
| 9 | A new Genesys Audio Connector API key + client secret, stored in Secret Manager |

## C. Genesys

| # | Request |
|---|---|
| 10 | A new Audio Connector integration pointing at the adapter copy's URL |
| 11 | A **staging** inbound flow + test DID (not the GECX route) that calls the Audio Connector with input variables:<br>`_agent_id = projects/mxs-maxisapp-prd/locations/us/apps/adk`<br>`caller_id = Call.Ani`<br>Failure path → queue |
| 12 | The **output variables Architect branches on** after the bot ends the call (escalation names, e.g. `intent`, `summary`, `language`) |

## D. Test data and business confirmations

| # | Request |
|---|---|
| 13 | Roaming test numbers that work on the staging gateway (the current ones return `NOT_A_MAXIS_NUMBER`) |
| 14 | Confirm the gateway checks that `line_ref` belongs to the `X-Caller-Id` account (other lines only come from the get-lines API) |
| 15 | The correct roaming day pass price: **RM29 or RM38** (the FAQ content disagrees) |
| 16 | Later: approval + test lines to turn the gateway write calls back on (buy pass, switch roaming, wrong pass); they're switched off in our code today |

## E. Later hardening (not needed now)

| # | Request |
|---|---|
| 17 | Turn the invoker IAM check on `maxis-voice-agent` back on; deploy our adapter patch that also sends a Google ID token (`X-Serverless-Authorization`); grant the adapter copy's service account `roles/run.invoker` on `maxis-voice-agent` |

Details and commands: `Documents/Genesys_Adapter_Rollout.md`, `Documents/Genesys_Adapter_Contract.md`.
