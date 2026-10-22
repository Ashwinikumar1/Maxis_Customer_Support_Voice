#!/usr/bin/env bash
# Rolls the observability image onto the EXISTING Cloud Run service.
#
# ../deploy.sh (unchanged) still owns APIs, IAM, the service account and the
# app's env vars. This script only:
#   1. builds observability/Dockerfile with Cloud Build;
#   2. deploys that image to the same service, adding the add-on's env vars
#      with --update-env-vars (all other settings and env vars are kept).
#
# Usage (from voice_agent/):  ./deploy.sh            # once, or when env/IAM change
#                             ./observability/deploy.sh
# NOTE: running ../deploy.sh again rebuilds the plain image and resets env vars;
# run this script after it to put the add-on back.
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "Missing .env - copy .env.example to .env first"; exit 1; }
set -a; source .env; set +a

PROJECT="$GOOGLE_CLOUD_PROJECT"
REGION="$CLOUD_RUN_REGION"
SERVICE="$CLOUD_RUN_SERVICE"
REPO="${OBS_ARTIFACT_REPO:-cloud-run-source-deploy}"   # the repo `gcloud run deploy --source` already created
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}-obs:${TAG}"

gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" >/dev/null 2>&1 || {
  echo "Service $SERVICE not found in $REGION - run ./deploy.sh once first"; exit 1; }

echo "== Build $IMAGE"
gcloud builds submit --project "$PROJECT" --region "$REGION" \
  --config observability/cloudbuild.yaml --substitutions "_IMAGE=$IMAGE" .

# Add-on settings. Values from .env win, so they can be tuned without editing this file.
OBS_ENV=(
  "LOG_FORMAT=${LOG_FORMAT:-json}"
  "LOG_LEVEL=${LOG_LEVEL:-INFO}"
  "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=${ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS:-false}"
  "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=${OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT:-NO_CONTENT}"
)
# Google's recommended GenAI semantic conventions: opt in from .env after a staging check.
if [[ -n "${OTEL_SEMCONV_STABILITY_OPT_IN:-}" ]]; then
  OBS_ENV+=("OTEL_SEMCONV_STABILITY_OPT_IN=$OTEL_SEMCONV_STABILITY_OPT_IN")
fi
# Tag every span, metric and log resource with the deployed commit.
OBS_ENV+=("OTEL_RESOURCE_ATTRIBUTES=cloud.region=$REGION,service.version=$TAG")
ENV_ARG="^@^$(IFS=@; echo "${OBS_ENV[*]}")"

echo "== Deploy $IMAGE -> $SERVICE"
gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" --update-env-vars "$ENV_ARG"

echo
echo "Logs (this revision): https://console.cloud.google.com/logs/query;query=resource.type%3D%22cloud_run_revision%22%0Aresource.labels.service_name%3D%22${SERVICE}%22?project=${PROJECT}"
echo "Traces:               https://console.cloud.google.com/traces/explorer?project=${PROJECT}"
