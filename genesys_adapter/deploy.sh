#!/usr/bin/env bash
# Deploys a copy of the Maxis Genesys Audio Connector adapter (ces-genesys-adapter-adk-stg)
# to Cloud Run, configured to forward calls to our ADK voice agent (CES_WS_URL).
#
# All settings come from .env (see .env.example).
# Usage:  cd genesys_adapter && ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

[[ -f .env ]] || { echo "Missing .env - copy .env.example to .env first"; exit 1; }
set -a; source .env; set +a

PROJECT="$GOOGLE_CLOUD_PROJECT"
REGION="$CLOUD_RUN_REGION"
SERVICE="$CLOUD_RUN_SERVICE"
SA="$CLOUD_RUN_SERVICE_ACCOUNT"

# Ensure the 1-line CES_WS_URL patch is present in src/ces_ws.py before deploying
grep -q 'CES_WS_URL' src/ces_ws.py || {
  echo "ERROR: src/ces_ws.py does not have the CES_WS_URL patch applied!"
  exit 1
}

# Ensure CES_WS_URL ends with a trailing slash (the adapter appends <location>, e.g. "us")
[[ "$CES_WS_URL" == */ ]] || CES_WS_URL="${CES_WS_URL}/"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
SERVICE_URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

echo "== Deploying $SERVICE -> $SERVICE_URL"
echo "   Backend CES_WS_URL:     $CES_WS_URL"
echo "   AUTH_TOKEN_SECRET_PATH: $AUTH_TOKEN_SECRET_PATH"
echo "   Service Account:        $SA"

ENV_VARS=(
  "CES_WS_URL=$CES_WS_URL"
  "AUTH_TOKEN_SECRET_PATH=$AUTH_TOKEN_SECRET_PATH"
)
ENV_ARG="^@^$(IFS=@; echo "${ENV_VARS[*]}")"

SECRET_ARGS=(
  --set-secrets "GENESYS_API_KEY=${GENESYS_API_KEY_SECRET}:latest,GENESYS_CLIENT_SECRET=${GENESYS_CLIENT_SECRET}:latest"
)

# Mirrors the existing ces-genesys-adapter-stg configuration:
# - ingress all, timeout 3600s, no CPU throttling, CPU boost enabled
# Note: org policy constraints/run.managed.requireInvokerIam blocks --no-invoker-iam-check
# until Maxis grants the org policy exception on this service.
gcloud run deploy "$SERVICE" \
  --project "$PROJECT" \
  --region "$REGION" \
  --source . \
  --service-account "$SA" \
  --ingress all \
  --allow-unauthenticated \
  --timeout "${CLOUD_RUN_TIMEOUT:-3600}" \
  --no-cpu-throttling \
  --cpu-boost \
  --cpu "${CLOUD_RUN_CPU:-1}" \
  --memory "${CLOUD_RUN_MEMORY:-512Mi}" \
  --min-instances "${CLOUD_RUN_MIN_INSTANCES:-1}" \
  --max-instances "${CLOUD_RUN_MAX_INSTANCES:-50}" \
  --concurrency "${CLOUD_RUN_CONCURRENCY:-80}" \
  --set-env-vars "$ENV_ARG" \
  "${SECRET_ARGS[@]}"

echo
echo "Adapter WebSocket (for Genesys Audio Connector Base URI): ${SERVICE_URL/https:/wss:}/"
echo "Liveness check:  curl -i $SERVICE_URL/health/liveness"
echo "Readiness check: curl -i $SERVICE_URL/health/readiness"
echo "Logs:            https://console.cloud.google.com/logs/query?project=${PROJECT}"
