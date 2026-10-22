#!/usr/bin/env bash
# Deploys the Maxis voice agent (UI + WebSocket API) to Cloud Run with OpenTelemetry
# (traces, metrics, logs -> Google Cloud).
# All settings come from .env (see .env.example).  Usage:  cd voice_agent && ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

[[ -f .env ]] || { echo "Missing .env - copy .env.example to .env first"; exit 1; }
set -a; source .env; set +a

# -----------------------------------------------------------------------------
# Dev-only guard (baseline CR-13, SC-11, IA-06)
# This script is for a developer's own project. It builds from local source,
# grants roles at project level and, by default, deploys a public service.
# Test and production must instead go through the pipeline
# (security/ci/cloudbuild-image.yaml): it scans the image, signs it, deploys by
# digest with Binary Authorization, and IAM comes from security/terraform.
# Set DEPLOY_ENV in .env; anything other than "dev" stops here, so the script
# can't be pointed at a shared environment by mistake.
# -----------------------------------------------------------------------------
if [[ "${DEPLOY_ENV:-dev}" != "dev" ]]; then
  echo "deploy.sh is for personal dev projects only (DEPLOY_ENV=${DEPLOY_ENV})."
  echo "Deploy test and prod through security/ci/cloudbuild-image.yaml."
  exit 1
fi

PROJECT="$GOOGLE_CLOUD_PROJECT"
REGION="$CLOUD_RUN_REGION"
SERVICE="$CLOUD_RUN_SERVICE"
SA="${CLOUD_RUN_SERVICE_ACCOUNT}@${PROJECT}.iam.gserviceaccount.com"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
# Cloud Run's deterministic URL: the only browser origin allowed to open /ws.
SERVICE_URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

echo "== APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com discoveryengine.googleapis.com storage.googleapis.com \
  cloudtrace.googleapis.com telemetry.googleapis.com monitoring.googleapis.com logging.googleapis.com --project "$PROJECT"

echo "== Service account (least privilege)"
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$CLOUD_RUN_SERVICE_ACCOUNT" --project "$PROJECT" \
    --display-name "Maxis voice agent (Cloud Run)"

# A new service account takes a few seconds to propagate in IAM: retry grants.
retry() { for i in 1 2 3 4 5 6; do "$@" >/dev/null 2>&1 && return 0; sleep 10; done; "$@"; }
for role in roles/aiplatform.user roles/discoveryengine.viewer roles/serviceusage.serviceUsageConsumer \
            roles/telemetry.tracesWriter roles/telemetry.metricsWriter roles/telemetry.logsWriter \
            roles/cloudtrace.agent roles/monitoring.metricWriter roles/logging.logWriter; do
  retry gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
    --role "$role" --condition None --quiet
done
# Guardrails (voice_agent/guardrails): Model Armor and Sensitive Data Protection, only when configured.
if [[ -n "${MODEL_ARMOR_PROMPT_TEMPLATE:-}${MODEL_ARMOR_RESPONSE_TEMPLATE:-}" ]]; then
  gcloud services enable modelarmor.googleapis.com --project "$PROJECT"
  retry gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
    --role roles/modelarmor.user --condition None --quiet
fi
if [[ -n "${DLP_INSPECT_TEMPLATE:-}${DLP_DEIDENTIFY_TEMPLATE:-}" ]]; then
  gcloud services enable dlp.googleapis.com --project "$PROJECT"
  retry gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
    --role roles/dlp.user --condition None --quiet
fi
if [[ -n "${GCS_BUCKET:-}" ]]; then
  retry gcloud storage buckets add-iam-policy-binding "gs://$GCS_BUCKET" \
    --member "serviceAccount:$SA" --role roles/storage.objectCreator --quiet
fi

# App settings passed to the container, straight from .env. HOST/PORT are set by
# the image/Cloud Run; ALLOWED_ORIGINS and OTel are forced for production.
ENV_VARS=(
  "GOOGLE_GENAI_USE_VERTEXAI=$GOOGLE_GENAI_USE_VERTEXAI"
  "GOOGLE_CLOUD_PROJECT=$PROJECT"
  "GOOGLE_CLOUD_LOCATION=$GOOGLE_CLOUD_LOCATION"
  "VOICE_AGENT_MODEL=$VOICE_AGENT_MODEL"
  "VOICE_NAME=$VOICE_NAME"
  "DEFAULT_TIMEZONE=$DEFAULT_TIMEZONE"
  "FAQ_SEARCH_ENGINE_ID=$FAQ_SEARCH_ENGINE_ID"
  "GCS_BUCKET=${GCS_BUCKET:-}"
  "GCS_LOG_PREFIX=$GCS_LOG_PREFIX"
  "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=$ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"
  "OTEL_TO_CLOUD=true"
  "OTEL_SERVICE_NAME=$OTEL_SERVICE_NAME"
  "ALLOWED_ORIGINS=$SERVICE_URL"
  "MODEL_ARMOR_PROMPT_TEMPLATE=${MODEL_ARMOR_PROMPT_TEMPLATE:-}"
  "MODEL_ARMOR_RESPONSE_TEMPLATE=${MODEL_ARMOR_RESPONSE_TEMPLATE:-}"
  "MODEL_ARMOR_MODE=${MODEL_ARMOR_MODE:-inspect}"
  "DLP_INSPECT_TEMPLATE=${DLP_INSPECT_TEMPLATE:-}"
  "DLP_DEIDENTIFY_TEMPLATE=${DLP_DEIDENTIFY_TEMPLATE:-}"
  "SAFETY_BLOCK_THRESHOLD=${SAFETY_BLOCK_THRESHOLD:-BLOCK_MEDIUM_AND_ABOVE}"
  "WRITE_TOOLS_ENABLED=${WRITE_TOOLS_ENABLED:-false}"
  "MAX_SESSION_SECONDS=${MAX_SESSION_SECONDS:-1800}"
  "SILENCE_TIMEOUT_SECONDS=${SILENCE_TIMEOUT_SECONDS:-120}"
  "SILENCE_RMS_THRESHOLD=${SILENCE_RMS_THRESHOLD:-500}"
  "IAP_AUDIENCE=${IAP_AUDIENCE:-}"
  "REQUIRE_IAP=${REQUIRE_IAP:-false}"
  "REQUIRE_PERSISTENT_SESSIONS=${REQUIRE_PERSISTENT_SESSIONS:-false}"
)
# Secrets come from Secret Manager, never plain env vars: the HMAC key for security events and,
# when used, the session database URL (secret names from `terraform output app_env`).
SECRETS=()
[[ -n "${SECURITY_EVENT_HMAC_KEY_SECRET:-}" ]] && SECRETS+=("SECURITY_EVENT_HMAC_KEY=${SECURITY_EVENT_HMAC_KEY_SECRET}:latest")
[[ -n "${SESSION_DB_URL_SECRET:-}" ]] && SECRETS+=("SESSION_DB_URL=${SESSION_DB_URL_SECRET}:latest")
SECRET_ARGS=()
(( ${#SECRETS[@]} )) && SECRET_ARGS=(--set-secrets "$(IFS=,; echo "${SECRETS[*]}")")

# Public demo by default. Behind the load balancer and IAP (security/terraform): set
# CLOUD_RUN_PUBLIC=false and CLOUD_RUN_INGRESS=internal-and-cloud-load-balancing.
AUTH_FLAG="--allow-unauthenticated"
[[ "${CLOUD_RUN_PUBLIC:-true}" == "false" ]] && AUTH_FLAG="--no-allow-unauthenticated"
if [[ "$AUTH_FLAG" == "--allow-unauthenticated" ]]; then
  # Say it out loud: anyone with the URL can open a call (and spend Gemini quota).
  echo "WARNING: deploying a PUBLIC service. Set CLOUD_RUN_PUBLIC=false once IAP is in front."
fi
ENV_ARG="^@^$(IFS=@; echo "${ENV_VARS[*]}")"   # '@' delimiter: values may contain commas

echo "== Deploy $SERVICE -> $SERVICE_URL"
# WebSocket voice calls need: long request timeout (max 60 min), session affinity,
# and CPU kept allocated so audio streaming, the transcript upload and the trace
# export keep running between requests (--no-cpu-throttling).
gcloud run deploy "$SERVICE" \
  --project "$PROJECT" --region "$REGION" --source . \
  --service-account "$SA" \
  "$AUTH_FLAG" --ingress "${CLOUD_RUN_INGRESS:-all}" \
  --timeout 3600 --session-affinity --no-cpu-throttling \
  --cpu 2 --memory 1Gi --concurrency 20 \
  --min-instances "$CLOUD_RUN_MIN_INSTANCES" --max-instances "$CLOUD_RUN_MAX_INSTANCES" \
  --set-env-vars "$ENV_ARG" "${SECRET_ARGS[@]}"

echo
echo "UI:     $SERVICE_URL"
echo "Traces:  https://console.cloud.google.com/traces/explorer?project=${PROJECT}"
echo "Metrics: https://console.cloud.google.com/monitoring/metrics-explorer?project=${PROJECT}"
echo "Logs:    https://console.cloud.google.com/logs/query?project=${PROJECT}"
