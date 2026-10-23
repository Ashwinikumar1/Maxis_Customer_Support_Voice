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

# -----------------------------------------------------------------------------
# Dev-only guard (baseline CR-13, SC-11, IA-06)
# This script is for a developer's own project. It builds from local source,
# grants roles at project level and, by default, deploys a public service.
# Test and production must instead go through the pipeline
# (security/ci/cloudbuild-build.yaml, then cloudbuild-deploy.yaml): they scan the image, sign it, deploy by
# digest with Binary Authorization, and IAM comes from security/terraform.
# Set DEPLOY_ENV in .env; anything other than "dev" stops here, so the script
# can't be pointed at a shared environment by mistake.
# -----------------------------------------------------------------------------
if [[ "${DEPLOY_ENV:-dev}" != "dev" ]]; then
  echo "deploy.sh is for personal dev projects only (DEPLOY_ENV=${DEPLOY_ENV})."
  echo "Deploy test and prod through security/ci/cloudbuild-build.yaml and cloudbuild-deploy.yaml."
  exit 1
fi

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
# Guardrails (voice_agent/guardrails): Model Armor and Sensitive Data Protection, only when configured.
if [[ -n "${MODEL_ARMOR_PROMPT_TEMPLATE:-}${MODEL_ARMOR_RESPONSE_TEMPLATE:-}" ]]; then
  gcloud services enable modelarmor.googleapis.com --project "$PROJECT"
  retry gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
    --role roles/modelarmor.user --condition None --quiet
fi
# Cloud DLP: for the guardrails' DLP templates, or REDACT_TRANSCRIPTS=dlp (streaming_core/redaction.py).
if [[ -n "${DLP_INSPECT_TEMPLATE:-}${DLP_DEIDENTIFY_TEMPLATE:-}" || "${REDACT_TRANSCRIPTS:-regex}" == "dlp" ]]; then
  gcloud services enable dlp.googleapis.com --project "$PROJECT"
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
  "TEST_CALLER_VERIFICATION_LEVEL=${TEST_CALLER_VERIFICATION_LEVEL:-2}"
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
  # Guardrails (voice_agent/guardrails); values from `terraform output app_env` in security/terraform/live.
  "MODEL_ARMOR_PROMPT_TEMPLATE=${MODEL_ARMOR_PROMPT_TEMPLATE:-}"
  "MODEL_ARMOR_RESPONSE_TEMPLATE=${MODEL_ARMOR_RESPONSE_TEMPLATE:-}"
  "MODEL_ARMOR_MODE=${MODEL_ARMOR_MODE:-inspect}"
  "DLP_INSPECT_TEMPLATE=${DLP_INSPECT_TEMPLATE:-}"
  "DLP_DEIDENTIFY_TEMPLATE=${DLP_DEIDENTIFY_TEMPLATE:-}"
  "SAFETY_BLOCK_THRESHOLD=${SAFETY_BLOCK_THRESHOLD:-BLOCK_MEDIUM_AND_ABOVE}"
  "WRITE_TOOLS_ENABLED=${WRITE_TOOLS_ENABLED:-}"   # empty: on with the mock gateway, off with a real one
  "MAX_WRITES_PER_CALL=${MAX_WRITES_PER_CALL:-}"   # empty: 3 with the mock gateway, 1 with a real one
  "MAX_SESSION_SECONDS=${MAX_SESSION_SECONDS:-1800}"
  "SILENCE_TIMEOUT_SECONDS=${SILENCE_TIMEOUT_SECONDS:-120}"
  "SILENCE_RMS_THRESHOLD=${SILENCE_RMS_THRESHOLD:-500}"
  "IAP_AUDIENCE=${IAP_AUDIENCE:-}"
  "REQUIRE_IAP=${REQUIRE_IAP:-false}"
  "IAP_KEYS_URL=${IAP_KEYS_URL:-}"
  "REQUIRE_PERSISTENT_SESSIONS=${REQUIRE_PERSISTENT_SESSIONS:-false}"
)
# The sampling rate only applies to ratio samplers; pass it only when it is set.
if [[ -n "${OTEL_TRACES_SAMPLER_ARG:-}" ]]; then
  ENV_VARS+=("OTEL_TRACES_SAMPLER_ARG=$OTEL_TRACES_SAMPLER_ARG")
fi
# Google's recommended GenAI semantic conventions: opt in from .env after a staging check.
if [[ -n "${OTEL_SEMCONV_STABILITY_OPT_IN:-}" ]]; then
  ENV_VARS+=("OTEL_SEMCONV_STABILITY_OPT_IN=$OTEL_SEMCONV_STABILITY_OPT_IN")
fi
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
# Public access also needs --no-invoker-iam-check where the org restricts IAM sharing to its
# domain (allUsers can't be granted run.invoker there).
AUTH_ARGS=("$AUTH_FLAG")
[[ "$AUTH_FLAG" == "--allow-unauthenticated" ]] && AUTH_ARGS+=(--no-invoker-iam-check)
if [[ "$AUTH_FLAG" == "--allow-unauthenticated" ]]; then
  # Say it out loud: anyone with the URL can open a call (and spend Gemini quota).
  echo "WARNING: deploying a PUBLIC service. Set CLOUD_RUN_PUBLIC=false once IAP is in front."
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
  "${AUTH_ARGS[@]}" --ingress "${CLOUD_RUN_INGRESS:-all}" \
  --timeout 3600 --session-affinity --no-cpu-throttling \
  --cpu 2 --memory 1Gi --concurrency 20 \
  --min-instances "$CLOUD_RUN_MIN_INSTANCES" --max-instances "$CLOUD_RUN_MAX_INSTANCES" \
  --set-env-vars "$ENV_ARG" "${SECRET_ARGS[@]}"

echo
echo "Note: the mock gateway keeps demo state in memory per instance. For consistent demos set"
echo "      CLOUD_RUN_MAX_INSTANCES=1 while GATEWAY_URL=mock."
echo "UI:     $SERVICE_URL"
echo "Traces:  https://console.cloud.google.com/traces/explorer?project=${PROJECT}"
echo "Metrics: https://console.cloud.google.com/monitoring/metrics-explorer?project=${PROJECT}"
echo "Logs:    https://console.cloud.google.com/logs/query?project=${PROJECT}"
