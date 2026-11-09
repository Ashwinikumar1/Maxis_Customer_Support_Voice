# client: local test client for the voice agent server

Talks to the [server](../voice_agent_server/README.md) **exactly like Genesys does**: same CES protocol on
`/ces/<location>`, same 8 kHz telephone audio, same Bearer token. What you hear here is what a
phone caller will hear. Runs on your laptop only; nothing here is deployed.

```
browser (mic, speaker, transcript) ──/ws──▶ client.py (127.0.0.1:8090) ──CES, MULAW 8k, Bearer──▶ server /ces
                                            converts audio, holds the token                      (local or Cloud Run)
```

| File | What |
|---|---|
| [`client.py`](client.py) | Serves the browser UI and relays each session to the server as one "phone call" |
| [`ces_adapter_sim.py`](ces_adapter_sim.py) | No browser: plays WAV files as the caller, saves the bot's audio (scripted / repeatable tests) |
| [`ces.py`](ces.py) | The CES messages the Genesys adapter sends (shared by both) |
| [`audio.py`](audio.py) | MULAW / PCM16 conversion and resampling (copy of `voice_agent_server/streaming_core/audio.py`) |
| [`static/`](static) | Browser UI |

## Setup

```bash
cp .env.example .env     # SERVER_URL + CES_BRIDGE_TOKEN (the same token the server checks)
uv sync
```

For Cloud Run, get the token from the secret the server mounts:
`gcloud secrets versions access latest --secret <CES_BRIDGE_TOKEN_SECRET>` (raw token or its JSON
both work).

## Browser test

```bash
uv run python client.py            # http://127.0.0.1:8090 → type the caller number → "Start talking"
```

- **Caller number** → sent as the CES `msisdn` variable (like Architect's `msisdn = Call.Ani`) →
  the server uses it as `X-Caller-Id` on the gateway.
- **Transcript / tool activity** appear only if the server has `CES_DEBUG_EVENTS=true`
  (local `.env.example` default; on Cloud Run deploy with `DEPLOY_CES_DEBUG_EVENTS=true`).
  Audio works either way.
- **End of call**: the activity panel shows `endSession → Genesys outputVariables {...}`, i.e.
  what Architect would receive.
- **Stop** = the caller hangs up (`sys.remote-call-disconnected` + `clientHalfClose`).

## Scripted test (WAV files)

```bash
say -o /tmp/q1.wav --data-format=LEI16@16000 "Hi, how much is the roaming pass for Japan?"
say -o /tmp/q2.wav --data-format=LEI16@16000 "Can I please speak to a person?"
uv run python ces_adapter_sim.py --wav /tmp/q1.wav --wav /tmp/q2.wav --msisdn 60123186001 --out bot_audio.wav
# against Cloud Run: add --url wss://<service-host>/ces/test (default: SERVER_URL from .env)
```

The simulator streams 20 ms frames in real time, speaks each WAV after the bot's reply, prints
events (`bot speaking`, `interruptionSignal`, `endSession -> outputVariables {...}`) and saves the
bot's audio to `--out`. Extra Architect variables: `--var language=ms`.

## Troubleshooting

| Message | Cause |
|---|---|
| `Server refused the call: HTTP 401` | `CES_BRIDGE_TOKEN` here ≠ the server's |
| `HTTP 403` | Cloud Run IAM / ingress blocks you (the service must allow unauthenticated; the token is checked in the app) |
| `HTTP 404` | `SERVER_URL` must end in `/ces/<something>` |
| `Could not reach the server` | Server not running / wrong host; for Cloud Run use `wss://` |
| `Server closed the call (code 1011)` | Server-side error: check the server logs (`CES bridge: call ... failed`) |
