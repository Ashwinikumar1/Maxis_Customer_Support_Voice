# adapter_test: the Genesys path, end to end, on your laptop

```
genesys_sim.py (fake Genesys) ──▶ ces-genesys-adapter (Google's code + CES_WS_URL patch) ──▶ server /ces
      records the call                 run_adapter.py, :8091                                   :8092
```

```bash
./run_e2e.sh                    # real agent (Gemini Live, FAQ, gateway from voice_agent_server/.env), two spoken turns
./run_e2e.sh q1.wav q2.wav      # your own caller turns (16-bit WAV, any rate; macOS: say -o q.wav --data-format=LEI16@16000 "...")
MODE=stub ./run_e2e.sh          # no Gemini: call, caller hang-up, Genesys probe
open recordings/call-*_mix.wav  # listen (the stereo file has caller left, bot right)
```

- First run clones the adapter into `.adapter/`, applies [`ces_ws_CES_WS_URL.patch`](ces_ws_CES_WS_URL.patch)
  (the one-line change Maxis needs) and installs its requirements (network needed).
- Throwaway credentials each run: the shared token uses the Secret Manager JSON shape
  (`{"access_token","expiry"}`) and is given to both the adapter (`AUTH_TOKEN_SECRET_PATH` read is
  replaced by a local file in `run_adapter.py`) and our server (`CES_BRIDGE_TOKEN`). Nothing is created in GCP.
- The fake Genesys client signs requests like Genesys (X-API-KEY + HMAC signature), sends `open` with
  `_agent_id` and `caller_id`, streams 20 ms PCMU frames, speaks each WAV when the bot goes quiet,
  and hangs up after the last reply (or answers the adapter's `disconnect`).
- Logs: `logs/server.log`, `logs/adapter.log`. Transcript: `voice_agent_server/transcripts/`.
- The adapter treats SIGTERM as "drain" and keeps running; the script force-stops it. If a run is
  interrupted: `lsof -nP -iTCP:8091 -sTCP:LISTEN` and kill it.
