# voice_agent: run, deploy and operate

The application behind the Maxis Customer Support Voice Agent. For the overview,
GCP architecture diagram and repository layout, see the [root README](../README.md).

```
Browser mic (PCM16 16k, b64 JSON) ─▶ server.py /ws ─▶ LiveVoiceSession ─▶ ADK run_live() ─▶ Gemini Live (Vertex AI)
                                                                              └─ search_knowledge_base ─▶ Vertex AI Search (FAQ)
Browser speaker / transcript / activity ◀─ JSON events ◀─ emit(...) ◀─ ADK events
On disconnect: TranscriptLogger.save() ─▶ gs://$GCS_BUCKET/$GCS_LOG_PREFIX/conversation_<id>_<ts>.json
```

## Run locally
```bash
gcloud auth application-default login     # when credentials expire
cp .env.example .env                      # first time only, then edit
uv sync
uv run python server.py                   # http://127.0.0.1:8080 → "Start talking"
```
Try: *"What roaming passes do you have?"*, *"How do I turn on roaming?"*,
*"Macam mana nak aktifkan roaming?"*

FAQ search latency check: `uv run python scripts/bench_faq_search.py`

## Configuration (`.env`)
Every setting is documented in [`.env.example`](.env.example). Both the local
server and `deploy.sh` read the same file.

| Setting | Purpose |
|---|---|
| `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` | Vertex AI project and region |
| `VOICE_AGENT_MODEL`, `VOICE_NAME` | Gemini Live model and prebuilt voice |
| `DEFAULT_TIMEZONE` | Used for the date in the prompt when the client sends none |
| `FAQ_SEARCH_ENGINE_ID` | Vertex AI Search app with the Maxis FAQ |
| `GCS_BUCKET`, `GCS_LOG_PREFIX` | Transcript destination (empty bucket = `./transcripts/`) |
| `OTEL_TO_CLOUD`, `OTEL_SERVICE_NAME`, `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` | OpenTelemetry export |
| `HOST`, `PORT`, `ALLOWED_ORIGINS` | Local server and allowed browser origins for `/ws` |
| `CLOUD_RUN_*` | Region, service name, service account, min/max instances (deploy only) |

## Deploy to Cloud Run (UI + WebSocket API in one service)
```bash
gcloud auth login && gcloud auth application-default login
./deploy.sh        # prints the UI URL and Trace / Metrics / Logs links
```
- `Dockerfile` serves `static/` and `/ws` on one port; `.dockerignore`/`.gcloudignore` keep `.env` out.
- `deploy.sh` enables the APIs, creates the `maxis-voice-agent` service account with least-privilege roles
  (`aiplatform.user`, `discoveryengine.viewer`, `serviceusage.serviceUsageConsumer`, Telemetry
  traces/metrics/logs writers, `storage.objectCreator` on the bucket), sets `ALLOWED_ORIGINS` to the
  service URL, and uses `--timeout 3600 --session-affinity --no-cpu-throttling` for long WebSocket calls.

## Observability (OpenTelemetry → Google Cloud)
`OTEL_TO_CLOUD=true` (forced on Cloud Run) sends traces to Cloud Trace, metrics to Cloud
Monitoring and ADK GenAI logs to Cloud Logging via the Telemetry API.
- One `voice_session` trace per call (tagged `session.id`) with `invoke_agent`, `execute_tool`
  and `model_turn` spans. `model_turn` carries `voice.time_to_first_audio_ms`, tool events,
  token usage and outcome.
- Metrics: `voice.time_to_first_audio` (histogram), `voice.model_turns` (counter), plus ADK GenAI metrics.
- Caller speech stays out of spans unless `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=true`.

## Next: Genesys Cloud telephony
`LiveVoiceSession` doesn't depend on the browser. A telephony endpoint only needs to:
1. accept the Genesys Audio Connector WebSocket (`open` → `opened`),
2. convert caller PCMU 8 kHz → PCM16 16 kHz → `session.send_audio()`,
3. in its `emit`, convert `audio` PCM16 24 kHz → PCMU 8 kHz, and send a barge-in on `interrupted`,
4. reuse the same agent, `TranscriptLogger` and Vertex AI config.

## Production notes
- `TODO(security)`: there is no auth on `/ws` yet (only an Origin check). Derive `user_id`
  from a verified identity, and put IAP or auth in front (`--allow-unauthenticated` is for the demo).
- Use a persistent SessionService instead of `InMemorySessionService`.
- Transcripts are PII: private bucket, retention policy, consider CMEK.
