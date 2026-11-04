# voice_agent: run, configure, test, deploy, operate

The application behind the Maxis Customer Support Voice Agent. For the overview, architecture,
agents and the Genesys design, see the [root README](../README.md).

```
Browser mic ──/ws (JSON, PCM16 16k)──────────────┐
                                                 ├─▶ Call ─▶ LiveVoiceSession ─▶ ADK run_live() ─▶ Gemini Live
Genesys ─▶ ces-genesys-adapter ─/ces (CES, MULAW)┘      │                           ├─ VertexAiSearchTool ─▶ FAQ data store
                                                        │                           └─ roaming tools ─▶ Maxis agent gateway
                                                        ├─ BridgeSound (clicks during tools / transfers)
                                                        └─ TranscriptLogger ─▶ gs://$GCS_BUCKET/$GCS_LOG_PREFIX/...
```

## Run locally

```bash
gcloud auth login --update-adc            # when credentials expire
cp .env.example .env                      # first time only, then edit
uv sync
uv run python server.py                   # http://127.0.0.1:8080 → "Start talking"
```

- Restart the server after changing static files (they are cached at startup).
- Port taken? `PORT=8081 ALLOWED_ORIGINS=http://127.0.0.1:8081,http://localhost:8081 uv run python server.py`
- The UI has a caller-number box. It accepts `DEMO_MSISDNS` only, unless `ALLOW_ANY_MSISDN=true`
  (testing only: the gateway trusts this number).
- FAQ search evidence is logged as `voice_agent.live: Vertex AI Search grounding: queries=[...]`
  and shown in the UI activity panel as `vertex_ai_search(...)`.

## Configuration (`.env`)

Every setting is documented in [`.env.example`](.env.example). The local server and `deploy.sh`
read the same file.

| Setting | Purpose |
|---|---|
| `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` | Vertex AI project and region for Gemini Live |
| `VOICE_AGENT_MODEL`, `VOICE_NAME`, `LANGUAGE_CODE` | Live model, prebuilt voice, speech language (empty = auto) |
| `TRANSCRIPTION_LANGUAGE_CODES` | Transcript language hints (default `en-US,ms-MY`) |
| `DEFAULT_TIMEZONE` | Date/greeting when the channel gives no timezone (telephony always uses this) |
| `FAQ_DATASTORE_ID` | Vertex AI Search data store the agent grounds on |
| `GATEWAY_URL`, `GATEWAY_AUTH`, `GATEWAY_TIMEOUT_S`, `GATEWAY_IMPERSONATE_SA`, `GATEWAY_API_KEY` | Maxis agent gateway. Each call sends `Authorization: Bearer <ID token>`, `X-Caller-Id`, `X-Conversation-Id` |
| `DEMO_MSISDNS`, `ALLOW_ANY_MSISDN` | Browser caller numbers (first = default); `ALLOW_ANY_MSISDN` is testing only |
| `BRIDGE_SOUND` | Keyboard clicks while a tool runs or agents transfer (`false` = silence) |
| `GCS_BUCKET`, `GCS_LOG_PREFIX` | Transcript destination (empty bucket = `./transcripts/`) |
| `OTEL_TO_CLOUD`, `OTEL_SERVICE_NAME`, `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` | OpenTelemetry export (caller speech in spans only if the last one is `true`) |
| `HOST`, `PORT`, `ALLOWED_ORIGINS` | Server bind and allowed browser origins for `/ws` |
| `BROWSER_CHANNEL_ENABLED` | `false` = no UI and no `/ws` (production telephony service) |
| `CES_BRIDGE_ENABLED` | `true` = accept the ces-genesys-adapter on `/ces/<location>` |
| `CES_BRIDGE_TOKEN` | Local only: bridge token (≥ 32 chars) or the adapter secret's JSON `{"access_token": ...}` |
| `CES_BRIDGE_TOKEN_SECRET` | Deploy only: Secret Manager secret mounted as `CES_BRIDGE_TOKEN` (same secret as the adapter's `AUTH_TOKEN_SECRET_PATH`) |
| `CES_BRIDGE_KICKSTART_TEXT` | First "caller" turn on `session_start` so the agent greets (default `Hello`) |
| `CLOUD_RUN_*` | Region, service name, service account, min/max instances (deploy only) |

## Endpoints

| Path | Who | Auth | Notes |
|---|---|---|---|
| `/`, `/static/*` | browser | none | Fixed allow-list of files, strict CSP. Off when `BROWSER_CHANNEL_ENABLED=false` |
| `/ws` | browser | `Origin` must be in `ALLOWED_ORIGINS` | JSON protocol documented in the `server.py` docstring |
| `/ces/<location>` | ces-genesys-adapter | `Authorization: Bearer <CES_BRIDGE_TOKEN>`, checked before the upgrade (401) | Only when `CES_BRIDGE_ENABLED=true`, else 404. Protocol in [`channels/ces_bridge.py`](channels/ces_bridge.py) |

## Genesys telephony bridge (`/ces`)

[`channels/ces_bridge.py`](channels/ces_bridge.py) answers the CES BidiRunSession protocol, so
the **existing** `ces-genesys-adapter` drives our ADK agent. The protocol table, output variables
and rollout steps are in the root README:
[Genesys telephony](../README.md#genesys-telephony-reusing-the-existing-adapter).

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

### Test the telephony bridge

**Offline** (no Gemini, no network; stubs the call and plays the adapter's exact messages):

```bash
uv run python scripts/test_ces_bridge.py      # prints "ALL CES BRIDGE CHECKS PASSED"
```

**Real call through `/ces`** (Gemini Live + real agents; the simulator plays the adapter):

```bash
# terminal 1: server with the bridge on
CES_BRIDGE_ENABLED=true CES_BRIDGE_TOKEN=<random ≥32 chars> PORT=8081 uv run python server.py

# terminal 2: caller turns as WAV files (macOS `say`, or any 16-bit WAV recording)
say -o /tmp/q1.wav --data-format=LEI16@16000 "Hi, how much is the roaming pass for Japan?"
say -o /tmp/q2.wav --data-format=LEI16@16000 "Can I please speak to a person?"
CES_BRIDGE_TOKEN=<same token> uv run python scripts/ces_adapter_sim.py \
    --url ws://127.0.0.1:8081/ces/us --wav /tmp/q1.wav --wav /tmp/q2.wav \
    --msisdn 60123186001 --out bot_audio.wav
```

The simulator streams 20 ms frames in real time, speaks each WAV after the bot's reply, prints
events (`bot speaking`, `interruptionSignal`, `endSession -> outputVariables {...}`) and saves the
bot's audio to `--out`. Extra Architect variables: `--var language=ms`.

**With the real adapter** (after the rollout steps): call the staging DID and check the logs for
`CES bridge: call <conversationId> started (msisdn ...XXXX, variables [...])` and
`... ended (outcome, outcome handoff)`.

## Deploy to Cloud Run

```bash
gcloud auth login && gcloud auth application-default login
./deploy.sh        # prints the UI URL and Trace / Metrics / Logs links
```

`deploy.sh`:
- enables the required APIs;
- creates the `maxis-voice-agent` service account with least-privilege roles:
  `aiplatform.user`, `discoveryengine.viewer`, `serviceusage.serviceUsageConsumer`, Telemetry
  traces/metrics/logs writers, `cloudtrace.agent`, `monitoring.metricWriter`, `logging.logWriter`,
  plus `storage.objectCreator` on the transcript bucket;
- if `CES_BRIDGE_TOKEN_SECRET` is set, grants `secretmanager.secretAccessor` on it and mounts it
  as `CES_BRIDGE_TOKEN`;
- deploys with `--timeout 3600 --session-affinity --no-cpu-throttling --concurrency 20`, because
  one WebSocket lasts the whole call;
- sets `ALLOWED_ORIGINS` to the service URL.

Not done by `deploy.sh` (ask the Maxis admins):
- `roles/run.invoker` on the gateway service for `maxis-voice-agent`;
- the adapter's second service and its `CES_WS_URL` / `AUTH_TOKEN_SECRET_PATH`;
- ingress restrictions.

`Dockerfile`: `python:3.12-slim`, non-root user, copies `streaming_core/`, `channels/`,
`voice_assistant/`, `static/` and `server.py` only (`scripts/` and `archive/` are excluded),
never `.env`.

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
| `/ces` returns 401 | Token mismatch: the adapter secret's `access_token` ≠ `CES_BRIDGE_TOKEN`, or token < 32 chars (also logged at startup) |
| `/ces` returns 404 | `CES_BRIDGE_ENABLED` not `true`, or the path isn't `/ces/<location>` (the adapter's `CES_WS_URL` must end with `/ces/`) |
| Adapter disconnects with "Missing required parameter: _agent_id" | Architect must pass `_agent_id = projects/<p>/locations/us/apps/adk` |
| Bot answers FAQs but every account tool says the number isn't available | No `msisdn` / `ani` input variable (or not 8–15 digits) → caller is `anonymous` |
| Gateway calls fail with 403 | Service account lacks `run.invoker` on the gateway |
| Gateway timeouts at night | Staging backends are down overnight (MYT); test in business hours |
| `CERTIFICATE_VERIFY_FAILED` locally | macOS CA bundle; `server.py` sets `SSL_CERT_FILE` from certifi, check the venv |
| Auth errors from Vertex AI | `gcloud auth login --update-adc` |

## Production checklist

- [ ] Telephony service: `BROWSER_CHANNEL_ENABLED=false`, `CES_BRIDGE_ENABLED=true`, token from Secret Manager.
- [ ] Restrict ingress (internal + Direct VPC egress from the adapter, or LB + Cloud Armor). `TODO(security)` in `deploy.sh`.
- [ ] Separate adapter service for the ADK bot; CX Studio adapter untouched.
- [ ] Architect failure path → queue; outputVariables agreed with the Maxis Genesys owner.
- [ ] `min-instances ≥ 1` (no cold start on a call), max instances sized for peak calls ÷ 20.
- [ ] Transcripts bucket private, retention policy, CMEK if required (PII).
- [ ] Persistent `SessionService` if calls must survive instance restarts.
- [ ] Rotate the bridge token: new secret version, then restart both services (the adapter caches the token until its `expiry`).
