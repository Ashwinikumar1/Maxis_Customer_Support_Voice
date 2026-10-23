#!/usr/bin/env bash
# Deploys the Maxis voice agent (UI + WebSocket API) to Cloud Run with OpenTelemetry
# (traces, metrics, logs -> Google Cloud) and the observability launcher.
# ONE script does everything, step by step: APIs -> service account + IAM ->
# bucket access -> build (./Dockerfile) + deploy with all env vars.
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
# Deployed version = short git commit (falls back to a timestamp outside a git clone).
VERSION="$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"

echo "== APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com discoveryengine.googleapis.com storage.googleapis.com \
  cloudtrace.googleapis.com telemetry.googleapis.com monitoring.googleapis.com logging.googleapis.com \
  dlp.googleapis.com --project "$PROJECT"

echo "== Service account (least privilege)"
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$CLOUD_RUN_SERVICE_ACCOUNT" --project "$PROJECT" \
    --display-name "Maxis voice agent (Cloud Run)"

# A new service account takes a few seconds to propagate in IAM: retry grants.
retry() { for i in 1 2 3 4 5 6; do "$@" >/dev/null 2>&1 && return 0; sleep 10; done; "$@"; }
# Telemetry roles: exactly the three Google's ADK instrumentation guide lists
# (https://docs.cloud.google.com/stackdriver/docs/instrumentation/ai-agent-adk).
# Three overlapping roles were removed - each one's permissions are already
# covered (checked in https://docs.cloud.google.com/iam/docs/roles-permissions/):
#   roles/cloudtrace.agent        - adds cloudtrace.traces.patch for the older Cloud Trace
#                                   API; ADK exports traces to telemetry.googleapis.com,
#                                   which needs only telemetry.traces.write (tracesWriter)
#   roles/telemetry.logsWriter    - only logging.logEntries.create, included in logging.logWriter
#   roles/telemetry.metricsWriter - only monitoring.timeSeries.create, included in
#                                   monitoring.metricWriter
# NOTE: this script only ADDS bindings; it never removes them. On a project that was
# deployed before this change, the three removed roles stay granted until removed
# by hand with `gcloud projects remove-iam-policy-binding`.
for role in roles/aiplatform.user roles/discoveryengine.viewer roles/serviceusage.serviceUsageConsumer \
            roles/telemetry.tracesWriter roles/monitoring.metricWriter roles/logging.logWriter; do
  retry gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
    --role "$role" --condition None --quiet
done
if [[ "${REDACT_TRANSCRIPTS:-regex}" == "dlp" ]]; then   # Cloud DLP redaction of transcripts
  retry gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
    --role roles/dlp.user --condition None --quiet
fi
# Real gateway: the runtime service account needs roles/run.invoker on the gateway's
# Cloud Run service, granted by the gateway's owner (Maxis), e.g.:
#   gcloud run services add-iam-policy-binding <gateway> --region asia-southeast1 \
#     --member serviceAccount:$SA --role roles/run.invoker --project <gateway project>
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
  "ROUTING_MODE=${ROUTING_MODE:-faq_first}"
  "GATEWAY_URL=${GATEWAY_URL:-mock}"
  "GATEWAY_AUTH=${GATEWAY_AUTH:-auto}"
  "GATEWAY_TIMEOUT_S=${GATEWAY_TIMEOUT_S:-30}"
  "ALLOW_TEST_CALLERS=${ALLOW_TEST_CALLERS:-true}"
  "MOCK_RESET_ON_CALL=${MOCK_RESET_ON_CALL:-true}"
  "MOCK_PASS_HISTORY=${MOCK_PASS_HISTORY:-true}"
  "REDACT_TRANSCRIPTS=${REDACT_TRANSCRIPTS:-regex}"
  "DLP_INFO_TYPES=${DLP_INFO_TYPES:-PHONE_NUMBER,EMAIL_ADDRESS,CREDIT_CARD_NUMBER,PERSON_NAME,STREET_ADDRESS,PASSPORT}"
  "OTEL_TO_CLOUD=true"
  "OTEL_SERVICE_NAME=$OTEL_SERVICE_NAME"
  "ALLOWED_ORIGINS=$SERVICE_URL"
  # [Observability] Logging and privacy settings. They used to be added by a
  # second script (observability/deploy.sh). They live here now because
  # --set-env-vars below DELETES every variable not in this list
  # (https://docs.cloud.google.com/run/docs/configuring/services/environment-variables),
  # so this one list must hold everything the service needs.
  # Values from .env win; the defaults are the safe ones.
  "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=${ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS:-false}"   # true = caller speech in spans (PII)
  "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=${OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT:-NO_CONTENT}"
  "LOG_FORMAT=${LOG_FORMAT:-json}"     # JSON lines -> structured, trace-linked Cloud Logging entries
  "LOG_LEVEL=${LOG_LEVEL:-INFO}"
  # [Observability #13] Trace sampling - every call by default; see .env.example for why.
  "OTEL_TRACES_SAMPLER=${OTEL_TRACES_SAMPLER:-parentbased_always_on}"
  # Tags every span, metric and log with the region and the deployed git commit,
  # so a problem can be tied to the release that introduced it.
  "OTEL_RESOURCE_ATTRIBUTES=cloud.region=$REGION,service.version=$VERSION"
)
# The sampling rate only applies to ratio samplers; pass it only when it is set.
if [[ -n "${OTEL_TRACES_SAMPLER_ARG:-}" ]]; then
  ENV_VARS+=("OTEL_TRACES_SAMPLER_ARG=$OTEL_TRACES_SAMPLER_ARG")
fi
# Google's recommended GenAI semantic conventions: opt in from .env after a staging check.
if [[ -n "${OTEL_SEMCONV_STABILITY_OPT_IN:-}" ]]; then
  ENV_VARS+=("OTEL_SEMCONV_STABILITY_OPT_IN=$OTEL_SEMCONV_STABILITY_OPT_IN")
fi
ENV_ARG="^@^$(IFS=@; echo "${ENV_VARS[*]}")"   # '@' delimiter: values may contain commas

echo "== Deploy $SERVICE -> $SERVICE_URL"
# WebSocket voice calls need: long request timeout (max 60 min), session affinity,
# and CPU kept allocated so audio streaming, the transcript upload and the trace
# export keep running between requests (--no-cpu-throttling).
# Public access (callers have no Google sign-in): --no-invoker-iam-check makes
# Cloud Run skip its IAM check. It is Google's recommended way when the
# organisation enforces domain-restricted sharing (iam.allowedPolicyMemberDomains),
# which silently blocks the allUsers grant behind --allow-unauthenticated:
# https://docs.cloud.google.com/run/docs/authenticating/public
# Seen on 23 Oct 2026: the service's IAM policy was empty and every request got
# Cloud Run's own 403. It fails only if the org enforces
# constraints/run.managed.requireInvokerIam (not enforced by default).
# /ws still checks the Origin header (server.py process_request).
gcloud run deploy "$SERVICE" \
  --project "$PROJECT" --region "$REGION" --source . \
  --service-account "$SA" \
  --allow-unauthenticated --no-invoker-iam-check \
  --timeout 3600 --session-affinity --no-cpu-throttling \
  --cpu 2 --memory 1Gi --concurrency 20 \
  --min-instances "$CLOUD_RUN_MIN_INSTANCES" --max-instances "$CLOUD_RUN_MAX_INSTANCES" \
  --set-env-vars "$ENV_ARG"

echo
echo "Note: the mock gateway keeps demo state in memory per instance. For consistent demos set"
echo "      CLOUD_RUN_MAX_INSTANCES=1 while GATEWAY_URL=mock."
echo "UI:     $SERVICE_URL"
echo "Traces:  https://console.cloud.google.com/traces/explorer?project=${PROJECT}"
echo "Metrics: https://console.cloud.google.com/monitoring/metrics-explorer?project=${PROJECT}"
echo "Logs:    https://console.cloud.google.com/logs/query?project=${PROJECT}"
