# Live Voice Agent (ADK + Gemini Live on Vertex AI)

A bidi-streaming voice agent: you talk in the browser, it answers out loud in real time,
and it shows live transcripts. It speaks **English and Malay**, uses **Google Search**,
and hands off to a **directions sub-agent** that calls the **Google Maps Routes API** for
real travel time, distance and steps. Each session's transcript is dumped to a **GCS bucket**
when the session ends.

```
Browser mic (PCM16 16k, b64 JSON) ─▶ server.py /ws ─▶ LiveVoiceSession ─▶ ADK run_live() ─▶ Gemini Live (Vertex)
                                                                              ├─ google_search (built-in)
                                                                              └─ directions_agent ─▶ get_directions ─▶ Routes API
Browser speaker / transcript / activity ◀─ JSON events ◀─ emit(...) ◀─ ADK events
On disconnect: TranscriptLogger.save() ─▶ gs://$GCS_BUCKET/$GCS_LOG_PREFIX/conversation_<id>_<ts>.json
```

## Files
| File | What |
|---|---|
| `voice_assistant/agent.py` | root agent (Google Search) + `directions_agent`, English/Malay rules |
| `voice_assistant/directions_tool.py` | `get_directions`: Routes API `computeRoutes` (ADC or `GOOGLE_MAPS_API_KEY`) |
| `streaming_core/live_session.py` | reusable live loop: client input in, `emit(type, **data)` out |
| `streaming_core/gcs_logger.py` | transcript dump to GCS (local `./transcripts/` if no bucket) |
| `server.py` | WebSocket server + static UI (like `streaming_service.py`) |
| `static/` | browser UI and audio worklets |

## Run (project `alpha-code-461805`)
Already done once: ADC login, Routes API enabled, and the bucket `alpha-code-461805-voice-transcripts` created.
```bash
gcloud auth application-default login     # when credentials expire
cd voice_agent
uv sync
uv run python server.py                   # http://127.0.0.1:8080 → "Start talking"
```
Try: *"What's the latest news about Maxis?"*, *"How long to drive from KL Sentral to KLIA?"*,
*"Berapa lama jalan kaki dari Bukit Bintang ke KLCC?"*

Smoke test the live path: `uv run adk-voice smoke voice_assistant --env-file .env --prompt "Drive time from KL Sentral to KLIA?"`

## Later: Genesys Cloud telephony
`LiveVoiceSession` doesn't depend on the browser. A telephony endpoint only needs to:
1. accept the Genesys Audio Connector WebSocket (`open` → `opened`),
2. convert caller PCMU 8 kHz → PCM16 16 kHz → `session.send_audio()`,
3. in its `emit`, convert `audio` PCM16 24 kHz → PCMU 8 kHz, send a barge-in on `interrupted`,
4. reuse the same agent, `TranscriptLogger` and Vertex config.

## Production notes
- `TODO(security)`: there is no auth on `/ws` yet (only an Origin check). Derive `user_id` from a verified identity.
- Persistent SessionService. HTTPS/WSS. A private bucket, since transcripts are PII; consider CMEK + retention.
- The service account needs `roles/aiplatform.user`, `roles/storage.objectCreator` on the bucket, and
  `roles/serviceusage.serviceUsageConsumer`. The Routes API is billed per request.

## Deploy to Cloud Run (UI + WebSocket API in one service, with OpenTelemetry)
```bash
gcloud auth login && gcloud auth application-default login
cd voice_agent && ./deploy.sh        # prints the UI URL and the Trace explorer link
```
- `Dockerfile` serves `static/` and `/ws` on one port; `.dockerignore`/`.gcloudignore` keep `.env` out.
- `deploy.sh` creates the `maxis-voice-agent` service account (Vertex AI, Vertex AI Search viewer,
  Telemetry traces/metrics/logs writers, bucket object creator), sets `ALLOWED_ORIGINS` to the service URL, and uses
  `--timeout 3600 --session-affinity --no-cpu-throttling` for long WebSocket calls.
- OpenTelemetry: `OTEL_TO_CLOUD=true` sends traces (Cloud Trace), metrics (Cloud Monitoring) and
  ADK GenAI logs (Cloud Logging) via the Telemetry API. One `voice_session` trace per call (tagged
  `session.id`) with `invoke_agent`, `execute_tool` and `model_turn` spans; `model_turn` carries
  `voice.time_to_first_audio_ms`, tool events, token usage and outcome. Metrics:
  `voice.time_to_first_audio` (histogram), `voice.model_turns` (counter) + ADK GenAI metrics.
  Caller speech is kept out of spans unless `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=true`.
- `--allow-unauthenticated` makes the demo UI public; put IAP or auth in front before real traffic.
