#!/usr/bin/env bash
# End-to-end test of the Genesys path, locally:
#   fake Genesys (genesys_sim.py) -> real ces-genesys-adapter (CES_WS_URL patch) -> our server /ces
# and a recording of the call you can listen to.
#
#   ./run_e2e.sh                     real agent (Gemini Live, FAQ, staging gateway), two spoken turns
#   ./run_e2e.sh q1.wav q2.wav       your own caller turns (16-bit WAV, any rate)
#   MODE=stub ./run_e2e.sh           no Gemini: protocol checks (call, hang-up, probe, wrong token)
#
# Random throwaway credentials; nothing is created in GCP. The real agent uses voice_agent_server/.env
# (Vertex AI, FAQ data store, gateway) exactly like `uv run python server.py`.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
MODE=${MODE:-real}
ADAPTER_DIR=${ADAPTER_DIR:-$ROOT/genesys_adapter}
OUT=$HERE/recordings; LOGS=$HERE/logs
mkdir -p "$OUT" "$LOGS"
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost

# --- 1. Adapter: uses $ROOT/genesys_adapter (creates .venv on first local run) ----------------
if [[ ! -x "$ADAPTER_DIR/.venv/bin/python" ]]; then
  echo "== setting up local adapter venv in $ADAPTER_DIR/.venv"
  uv venv -q --python 3.14 "$ADAPTER_DIR/.venv" && uv pip install -q --python "$ADAPTER_DIR/.venv/bin/python" -r "$ADAPTER_DIR/requirements.txt" || exit 1
fi
grep -q 'CES_WS_URL' "$ADAPTER_DIR/src/ces_ws.py" || { echo "adapter not patched (CES_WS_URL)"; exit 1; }
APY=$ADAPTER_DIR/.venv/bin/python
SPY=$ROOT/voice_agent_server/.venv/bin/python
for port in 8091 8092; do
  if lsof -nP -iTCP:$port -sTCP:LISTEN >/dev/null 2>&1; then
    echo "port $port is in use (a previous run?): lsof -nP -iTCP:$port -sTCP:LISTEN"; exit 1
  fi
done

# --- 2. Throwaway credentials: shared token (same JSON as the Secret Manager secret) + Genesys key
TOKEN=$($APY -c 'import secrets;print(secrets.token_urlsafe(48))')
export GENESYS_API_KEY=$($APY -c 'import secrets;print(secrets.token_hex(16))')
export GENESYS_CLIENT_SECRET=$($APY -c 'import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())')
$APY -c "import json;print(json.dumps({'access_token':'$TOKEN','expiry':4102444800000}))" > "$LOGS/token.json"
chmod 600 "$LOGS/token.json"

# --- 3. Our server on :8092 (real or stub), adapter on :8091 ----------------------------------
if [[ $MODE == stub ]]; then
  SERVER_CMD=("$SPY" "$HERE/run_server_stub.py")
else
  SERVER_CMD=("$SPY" "$ROOT/voice_agent_server/server.py")
fi
# run from voice_agent_server/ so transcripts land in voice_agent_server/transcripts like a normal local run
(cd "$ROOT/voice_agent_server" && SERVER_DIR=$ROOT/voice_agent_server PORT=8092 OTEL_TO_CLOUD=false CES_DEBUG_EVENTS=false \
  CES_BRIDGE_TOKEN="$(cat "$LOGS/token.json")" exec "${SERVER_CMD[@]}") > "$LOGS/server.log" 2>&1 &
PS=$!
ADAPTER_DIR=$ADAPTER_DIR PORT=8091 LOCAL_TOKEN_FILE=$LOGS/token.json CES_WS_URL=ws://127.0.0.1:8092/ces/ \
  "$APY" "$HERE/run_adapter.py" > "$LOGS/adapter.log" 2>&1 &
PA=$!
# the adapter treats SIGTERM as "drain" (Cloud Run behaviour) and keeps running: force-stop it
trap 'kill $PS 2>/dev/null; kill -9 $PA 2>/dev/null; wait 2>/dev/null' EXIT
for _ in $(seq 1 60); do
  curl -s --noproxy '*' -o /dev/null http://127.0.0.1:8092/healthz && curl -s --noproxy '*' -o /dev/null http://127.0.0.1:8091/health/liveness && break
  sleep 0.5
done

STAMP=$(date +%Y%m%d-%H%M%S)
if [[ $MODE == stub ]]; then
  say -o "$LOGS/stub_q.wav" --data-format=LEI16@16000 "Hello"
  for sc in call hangup probe; do
    echo "===== $sc"
    "$APY" "$HERE/genesys_sim.py" --scenario "$sc" --wav "$LOGS/stub_q.wav" --out "$OUT/stub-$sc-$STAMP.wav" | tail -8
  done
else
  # --- 4. Caller turns: given WAVs, or two spoken with macOS `say` ---------------------------
  WAVS=("$@")
  if [[ ${#WAVS[@]} -eq 0 ]]; then
    say -o "$LOGS/q1.wav" --data-format=LEI16@16000 "Hi, how much is the roaming day pass for Japan?"
    say -o "$LOGS/q2.wav" --data-format=LEI16@16000 "Okay, thank you. That's all I needed. Bye."
    WAVS=("$LOGS/q1.wav" "$LOGS/q2.wav")
  fi
  ARGS=(); for w in "${WAVS[@]}"; do ARGS+=(--wav "$w"); done
  echo "===== real call through the adapter"
  "$APY" "$HERE/genesys_sim.py" "${ARGS[@]}" --out "$OUT/call-$STAMP.wav" --timeout 240
fi

sleep 2
echo "===== our server"
grep -E 'CES bridge|Session .* (started|closed)|grounding|Gateway|ERROR|Traceback|Error' "$LOGS/server.log" | sed 's/^[0-9-]* //' | tail -25
echo "===== adapter"
grep -o '"message": "[^"]*\(CES\|disconnect\|endSession\|Interruption\|opened\)[^"]*"' "$LOGS/adapter.log" | sort | uniq -c | head -20
echo "===== recordings: $OUT"; ls -1t "$OUT" | head -4
