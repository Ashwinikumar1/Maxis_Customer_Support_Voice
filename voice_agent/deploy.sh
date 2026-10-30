#!/usr/bin/env bash
# Deploys the Maxis voice agent (UI + WebSocket API) to Cloud Run with OpenTelemetry
# (traces, metrics, logs -> Google Cloud).
# All settings come from .env (see .env.example).  Usage:  cd voice_agent && ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

[[ -f .env ]] || { echo "Missing .env - copy .env.example to .env first"; exit 1; }
set -a; source .env; set +a

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

# Cloud Build uses the default Compute Engine service account for `--source .` builds;
# in projects where automatic Editor grants are disabled, it needs Cloud Build permissions.
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
retry gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$COMPUTE_SA" \
  --role roles/cloudbuild.builds.builder --condition None --quiet

for role in roles/aiplatform.user roles/discoveryengine.viewer roles/serviceusage.serviceUsageConsumer \
            roles/telemetry.tracesWriter roles/telemetry.metricsWriter roles/telemetry.logsWriter \
            roles/cloudtrace.agent roles/monitoring.metricWriter roles/logging.logWriter; do
  retry gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
    --role "$role" --condition None --quiet
done
if [[ -n "${GCS_BUCKET:-}" ]]; then
  echo "== GCS bucket (gs://$GCS_BUCKET)"
  gcloud storage buckets describe "gs://$GCS_BUCKET" --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud storage buckets create "gs://$GCS_BUCKET" --project "$PROJECT" \
      --location "$REGION" --uniform-bucket-level-access
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
  "LANGUAGE_CODE=${LANGUAGE_CODE:-}"
  "DEFAULT_TIMEZONE=$DEFAULT_TIMEZONE"
  "FAQ_SEARCH_ENGINE_ID=$FAQ_SEARCH_ENGINE_ID"
  "GATEWAY_URL=${GATEWAY_URL:-}"
  "GATEWAY_TIMEOUT_S=${GATEWAY_TIMEOUT_S:-5}"
  "DEMO_MSISDNS=${DEMO_MSISDNS:-60123000005,60123000001}"
  "GCS_BUCKET=${GCS_BUCKET:-}"
  "GCS_LOG_PREFIX=$GCS_LOG_PREFIX"
  "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=$ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"
  "OTEL_TO_CLOUD=true"
  "OTEL_SERVICE_NAME=$OTEL_SERVICE_NAME"
  "ALLOWED_ORIGINS=$SERVICE_URL"
)
ENV_ARG="^@^$(IFS=@; echo "${ENV_VARS[*]}")"   # '@' delimiter: values may contain commas

echo "== Deploy $SERVICE -> $SERVICE_URL"
# WebSocket voice calls need: long request timeout (max 60 min), session affinity,
# and CPU kept allocated so audio streaming, the transcript upload and the trace
# export keep running between requests (--no-cpu-throttling).
gcloud run deploy "$SERVICE" \
  --project "$PROJECT" --region "$REGION" --source . \
  --service-account "$SA" \
  --allow-unauthenticated \
  --timeout 3600 --session-affinity --no-cpu-throttling \
  --cpu 2 --memory 1Gi --concurrency 20 \
  --min-instances "$CLOUD_RUN_MIN_INSTANCES" --max-instances "$CLOUD_RUN_MAX_INSTANCES" \
  --set-env-vars "$ENV_ARG"

echo
echo "UI:     $SERVICE_URL"
echo "Traces:  https://console.cloud.google.com/traces/explorer?project=${PROJECT}"
echo "Metrics: https://console.cloud.google.com/monitoring/metrics-explorer?project=${PROJECT}"
echo "Logs:    https://console.cloud.google.com/logs/query?project=${PROJECT}"
