# server: run, configure, test, deploy, operate

The voice agent **server** (runs on Cloud Run). It has no UI: every call comes in on
`/ces/<location>` using the CES protocol that the Genesys adapter speaks. For testing, the
[client](../voice_agent_client/README.md) speaks the same protocol from your laptop. For the overview, see
the [root README](../README.md).

```
test client (../client) ─┐
                         ├─/ces (CES, MULAW 8k, Bearer)─▶ Call ─▶ LiveVoiceSession ─▶ ADK run_live() ─▶ Gemini Live
Genesys ─▶ ces-adapter ──┘                                │                           ├─ VertexAiSearchTool ─▶ FAQ data store
                                                          │                           └─ roaming tools ─▶ Maxis agent gateway
                                                          ├─ BridgeSound (clicks during tools / transfers)
                                                          └─ TranscriptLogger ─▶ gs://$GCS_BUCKET/$GCS_LOG_PREFIX/...
```

## Run locally

```bash
gcloud auth login --update-adc            # when credentials expire
cp .env.example .env                      # first time only, then edit (set CES_BRIDGE_TOKEN)
uv sync
uv run python server.py                   # ws://127.0.0.1:8080/ces/<location>, GET /healthz
```

Then talk to it with the client: `cd ../client && uv run python client.py` → http://127.0.0.1:8090.

- FAQ search evidence is logged as `voice_agent.live: Vertex AI Search grounding: queries=[...]`
  and shown in the client's activity panel as `vertex_ai_search(...)` (needs `CES_DEBUG_EVENTS=true`).

## Configuration (`.env`)

Every setting is documented in [`.env.example`](.env.example). The local server and `deploy.sh`
read the same file.

| Setting | Purpose |
|---|---|
| `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` | Vertex AI project and region for Gemini Live |
| `VOICE_AGENT_MODEL`, `VOICE_NAME`, `LANGUAGE_CODE` | Live model, prebuilt voice, speech language (empty = auto) |
| `TRANSCRIPTION_LANGUAGE_CODES` | Transcript language hints (default `en-US,ms-MY`) |
| `DEFAULT_TIMEZONE` | Date/greeting when the caller gives no timezone (Genesys never does; the test client does) |
| `FAQ_DATASTORE_ID` | Vertex AI Search data store the agent grounds on |
| `GATEWAY_URL`, `GATEWAY_AUTH`, `GATEWAY_TIMEOUT_S`, `GATEWAY_IMPERSONATE_SA`, `GATEWAY_API_KEY` | Maxis agent gateway. Each call sends `Authorization: Bearer <ID token>`, `X-Caller-Id`, `X-Conversation-Id` |
| `BRIDGE_SOUND` | Keyboard clicks while a tool runs or agents transfer (`false` = silence) |
| `GCS_BUCKET`, `GCS_LOG_PREFIX` | Transcript destination (empty bucket = `./transcripts/`) |
| `OTEL_TO_CLOUD`, `OTEL_SERVICE_NAME`, `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` | OpenTelemetry export (caller speech in spans only if the last one is `true`) |
| `HOST`, `PORT` | Server bind (Cloud Run sets these) |
| `CES_BRIDGE_TOKEN` | Local only: `/ces` token (≥ 32 chars) or the adapter secret's JSON `{"access_token": ...}`. Missing → every call rejected |
| `CES_BRIDGE_TOKEN_SECRET` | Deploy only (required): Secret Manager secret mounted as `CES_BRIDGE_TOKEN` (same secret as the adapter's `AUTH_TOKEN_SECRET_PATH`) |
| `CES_BRIDGE_KICKSTART_TEXT` | First "caller" turn on `session_start` so the agent greets (default `Hello`) |
| `CES_DEBUG_EVENTS` | `true` = send transcripts / tool calls to a test client that asks for them. PII: `false` on prod. `deploy.sh` uses `DEPLOY_CES_DEBUG_EVENTS` (default `false`) |
| `CLOUD_RUN_*` | Region, service name, service account, min/max instances (deploy only) |

## Endpoints

| Path | Who | Auth | Notes |
|---|---|---|---|
| `/ces/<location>` | ces-genesys-adapter, test client | `Authorization: Bearer <CES_BRIDGE_TOKEN>`, checked before the upgrade (401) | Protocol in [`channels/ces_bridge.py`](channels/ces_bridge.py) |
| `/healthz` | uptime checks | none | `200 ok`, no data |
| anything else | | | 404 |

## Genesys telephony bridge (`/ces`)

> [!IMPORTANT]
> The phone channel reuses **the same `ces-genesys-adapter` that GECX (Gemini Enterprise for CX /
> CX Agent Studio) uses** with Genesys. The Genesys setup and adapter code stay the same; the
> adapter's backend URL points here instead of to GECX, and our **custom ADK code** answers the
> same CES protocol.

[`channels/ces_bridge.py`](channels/ces_bridge.py) answers the CES BidiRunSession protocol, so
the **existing** `ces-genesys-adapter` drives our ADK agent. AudioHook (the
`genesys-audio-connector` service) can't be used for the bot: it is listen-only (Agent Assist).

### Protocol mapping

| Adapter → us | Our bridge |
|---|---|
| `{"config": {"session": "<app>/sessions/<conversationId>"}}` | `CallInfo.conversation_id` = ADK `session_id` and `X-Conversation-Id` |
| `{"realtimeInput": {"variables": {...}}}` (Architect input variables) | Validated → `CallInfo.channel_vars` → session state `channel_vars` |
| variable `msisdn`, `caller_id` (what the GECX flow sets), `ani` or `telephony-caller-id`, e.g. `tel:+60123456789` | Digits only → `CallInfo.msisdn` = ADK `user_id` and `X-Caller-Id`. Missing → `anonymous` (FAQ works, account tools refuse) |
| `{"realtimeInput": {"event": {"event": "session_start"}}}` or `text` | Start the call; send kickstart text so the agent greets |
| `{"realtimeInput": {"audio": "<b64 MULAW 8k>"}}` | MULAW → PCM16 16 kHz → `call.send_audio()` |
| `{"realtimeInput": {"dtmf": "5"}}` | `call.send_text("[The caller pressed 5 on the keypad]")` |
| `sys.remote-call-disconnected` event / `clientHalfClose` | Caller hung up → `endSession` (no params) → close |

| Us → adapter | Meaning |
|---|---|
| `{"sessionOutput": {"audio": "<b64 MULAW 8k>"}}` | Bot speech (the adapter paces it to Genesys) |
| `{"interruptionSignal": {}}` | Barge-in: the adapter clears its queued audio |
| `{"endSession": {"metadata": {"params": {...}}}}` + close 1000 | Call over: the adapter plays out queued audio, then sends Genesys `disconnect` (completed) with params as **outputVariables** |
| close 1011 | Internal error: the adapter sends `disconnect` (error), so Architect takes its failure path |

### Output variables for Architect (flat strings)

Only `endSession.metadata.params` reaches Genesys. Full adapter contract, and how this
compares with the live GECX bot: [Genesys_Adapter_Contract.md](../Documents/Genesys_Adapter_Contract.md).

| Variable | When | Example |
|---|---|---|
| `outcome` | the agent decided | `handoff` or `end` |
| `session_escalated` | always (GECX name) | `true` on handoff, else `false` |
| `customer_request` | handoff (GECX name) | what the caller wanted but couldn't get done |
| `handoff_reason`, `summary` | handoff | why, and what the caller wanted (for the human agent) |
| `use_case`, `destination`, `pass_name` | handoff, when known | `buy_a_pass`, `Japan`, `Asia 7-day pass` |
| `end_reason` | end | `resolved`, `caller_done`, `no_response`, `unsupported_language` |

> Escalation (`escalateToAgent`) is placeholder code and **not working yet**. These variables
> are only sent once it's fixed and tested.

### Rollout steps (waiting on permission)

1. **Adapter, one line** in `src/ces_ws.py`:
   `_BASE_WS_URL = os.getenv("CES_WS_URL", "wss://ces.googleapis.com/ws/google.cloud.ces.v1.SessionService/BidiRunSession/locations/")`.
   Deploy it as a **second service from the same image**: repointing the shared
   `ces-genesys-adapter-stg` would break the GECX bot. Set `CES_WS_URL=wss://<voice agent host>/ces/`
   and `AUTH_TOKEN_SECRET_PATH=<token secret>`.
2. **Token secret**: JSON `{"access_token": "<random ≥ 32 chars>", "expiry": <far-future epoch ms>}`.
   Grant `secretAccessor` to both service accounts.
3. **Deploy the voice agent** with `CES_BRIDGE_TOKEN_SECRET=<secret>` (and
   `DEPLOY_CES_DEBUG_EVENTS` unset, i.e. `false`, on the production service).
4. **Architect** (staging flow + DID first): Call Audio Connector with input variables
   `_agent_id = projects/<p>/locations/us/apps/adk` (the adapter requires it; only `us` is used, as
   the URL suffix) and `msisdn = Call.Ani`. Branch on `outcome`; the failure path goes to a queue.
5. **End-to-end staging call**, then the [production checklist](#production-checklist).

Lifecycle of one adapter connection:

1. **Setup** (≤ 10 s, else close 1008): `config` → session id; `variables` → `channel_vars` and
   MSISDN; `session_start` / `text` / first audio → start the `Call` (+ kickstart text).
2. **Streaming**: three tasks run together. Upstream reads caller audio, DTMF and hang-up; the
   model task streams bot audio and `interruptionSignal`; the third waits for `call.ended`.
3. **End**: whichever finishes first decides.
   - Agent outcome (`escalateToAgent` / `endCall`): send `endSession` with output variables,
     close 1000.
   - Caller hung up: send `endSession` with no params, close 1000.
   - Error, or Live ended without an outcome: close 1011, so the adapter sends `disconnect`
     (error) and Architect takes its failure path.
   - Always: `call.finish()` closes Gemini Live and saves the transcript once.

Session state the agent can use: `msisdn`, `caller_id`, `conversation_id`, `channel`
(`genesys`), `channel_vars` (all Architect variables, strings). Tools read them through
`tool_context.state`.

### Test-client extras (Genesys never sends these)

| Client → us | Effect |
|---|---|
| variable `timezone` | Caller's timezone for the greeting and the date in the prompt |
| variable `debug_events = "true"` | If the server has `CES_DEBUG_EVENTS=true`: `{"debugEvent": {"type": "transcript" \| "tool_call" \| "tool_result" \| "active_agent" \| "turn_complete" \| "call_outcome", ...}}` |
| `{"realtimeInput": {"text": "..."}}` after setup | Typed caller message |

### Test

**Offline** (no Gemini, no network; stubs the call and plays the adapter's exact messages):

```bash
uv run python scripts/test_ces_bridge.py      # prints "ALL CES BRIDGE CHECKS PASSED"
```

**Real calls** (Gemini Live + real agents): use the [client](../voice_agent_client/README.md), either the
browser UI (`client.py`) or the WAV-driven adapter simulator (`ces_adapter_sim.py`). Both work
against a local server or Cloud Run.

**With the real adapter** (after the rollout steps): call the staging DID and check the logs for
`CES bridge: call <conversationId> started (msisdn ...XXXX, variables [...])` and
`... ended (outcome, outcome handoff)`.

## Deploy to Cloud Run

```bash
gcloud auth login && gcloud auth application-default login
./deploy.sh                                # production-style: no debug events
DEPLOY_CES_DEBUG_EVENTS=true ./deploy.sh   # dev/test service: transcripts for the test client
```

`deploy.sh`:
- enables the required APIs;
- creates the `maxis-voice-agent` service account with least-privilege roles:
  `aiplatform.user`, `discoveryengine.viewer`, `serviceusage.serviceUsageConsumer`, Telemetry
  traces/metrics/logs writers, `cloudtrace.agent`, `monitoring.metricWriter`, `logging.logWriter`,
  plus `storage.objectCreator` on the transcript bucket;
- requires `CES_BRIDGE_TOKEN_SECRET`, grants `secretmanager.secretAccessor` on it and mounts it
  as `CES_BRIDGE_TOKEN`;
- deploys with `--timeout 3600 --session-affinity --no-cpu-throttling --concurrency 20`, because
  one WebSocket lasts the whole call;
- prints the `wss://.../ces/<location>` URL and the matching client command.

Not done by `deploy.sh` (ask the Maxis admins):
- `roles/run.invoker` on the gateway service for `maxis-voice-agent`;
- the adapter's second service and its `CES_WS_URL` / `AUTH_TOKEN_SECRET_PATH`;
- ingress restrictions.

`Dockerfile`: `python:3.12-slim`, non-root user, copies `streaming_core/`, `channels/`,
`voice_assistant/` and `server.py` only (`scripts/` is excluded), never `.env`.

## Observability (OpenTelemetry → Google Cloud)

`OTEL_TO_CLOUD=true` (forced on Cloud Run) sends traces to Cloud Trace, metrics to Cloud
Monitoring and ADK GenAI logs to Cloud Logging through the Telemetry API.

- One `voice_session` trace per call (attribute `session.id` = transcript / Genesys conversation
  id) with `invoke_agent`, `execute_tool` and `model_turn` spans. `model_turn` carries
  `voice.time_to_first_audio_ms`, tool events, token usage and outcome.
- Metrics: `voice.time_to_first_audio` (histogram), `voice.model_turns` (counter), ADK GenAI metrics.
- Useful log lines: `Session <id> started (genesys, ..., msisdn ...1234)`,
  `CES bridge: call <id> ended (<reason>, outcome <handoff|end|none>)`,
  `CES bridge: rejected unauthenticated connection`, `Gateway <operation> failed: ...`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `/ces` returns 401 | Token mismatch: the adapter secret's / client's `access_token` ≠ server `CES_BRIDGE_TOKEN`, or token < 32 chars (also logged at startup) |
| `/ces` returns 404 | The path isn't `/ces/<location>` (the adapter's `CES_WS_URL` must end with `/ces/`) |
| Test client hears the bot but shows no transcript | Server has `CES_DEBUG_EVENTS=false` (default on `deploy.sh`) |
| Adapter disconnects with "Missing required parameter: _agent_id" | Architect must pass `_agent_id = projects/<p>/locations/us/apps/adk` |
| Bot answers FAQs but every account tool says the number isn't available | No `msisdn` / `ani` input variable (or not 8–15 digits) → caller is `anonymous` |
| Gateway calls fail with 403 | Service account lacks `run.invoker` on the gateway |
| Gateway timeouts at night | Staging backends are down overnight (MYT); test in business hours |
| `CERTIFICATE_VERIFY_FAILED` locally | macOS CA bundle; `server.py` sets `SSL_CERT_FILE` from certifi, check the venv |
| Auth errors from Vertex AI | `gcloud auth login --update-adc` |

## Production checklist

- [ ] Production service: `CES_DEBUG_EVENTS=false` (default in `deploy.sh`), token from Secret Manager.
- [ ] Restrict ingress (internal + Direct VPC egress from the adapter, or LB + Cloud Armor). `TODO(security)` in `deploy.sh`.
- [ ] Separate adapter service for the ADK bot; CX Studio adapter untouched.
- [ ] Architect failure path → queue; outputVariables agreed with the Maxis Genesys owner.
- [ ] `min-instances ≥ 1` (no cold start on a call), max instances sized for peak calls ÷ 20.
- [ ] Transcripts bucket private, retention policy, CMEK if required (PII).
- [ ] Persistent `SessionService` if calls must survive instance restarts.
- [ ] Rotate the bridge token: new secret version, then restart both services (the adapter caches the token until its `expiry`).
