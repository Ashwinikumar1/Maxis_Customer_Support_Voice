# Maxis voice agent: one container serves the browser UI (static/) and the
# WebSocket voice API (/ws) on the same port. Built for Cloud Run by ./deploy.sh
# (`gcloud run deploy --source .` builds with this Dockerfile when it is present:
# https://docs.cloud.google.com/run/docs/deploying-source-code).
#
# [Observability] This is the ONLY image. It includes the observability/ package
# (JSON logs linked to traces, span errors, FAQ tool spans, clean SIGTERM
# shutdown, session metrics) and starts through it. Earlier there was a second
# image (observability/Dockerfile) because existing files could not be edited;
# with one image nobody can deploy the app without its logging and tracing.
#
# Supply-chain rules this file follows (baseline SC-05, CR-11):
# - Every image is pinned by digest (@sha256:...), not only by tag. A tag such as
#   "3.12-slim" is moved by its publisher whenever they rebuild, so the same
#   Dockerfile could otherwise produce a different image tomorrow, including one
#   that was tampered with upstream. A digest names exactly one image forever.
#   The tag is kept next to it for readers; when both are given, the digest
#   decides which image is used.
# - Python packages install from uv.lock with --locked: exact versions and
#   hashes, and the build fails if uv.lock no longer matches pyproject.toml
#   (uv docs: --frozen would use the lock *without* checking it is up to date).
# - The app runs as an unprivileged user, and no .env is ever copied in.
#
# To update the base images (monthly, or when a scan finds a critical issue):
#   docker buildx imagetools inspect python:3.12-slim       -> "Digest:" line
#   docker buildx imagetools inspect ghcr.io/astral-sh/uv:<version>
# then replace the digests below and let the pipeline scan the new image
# (security/ci/cloudbuild-deploy.yaml) before it is deployed.

# uv (the Python package installer) comes from its official image, pinned by
# version and digest. Before, the build ran `pip install uv` with no version, so
# every build could pull a different, unreviewed installer.
FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
# Copy only the uv binary out of the pinned image: nothing else from it ends up
# in the final image, and pip is not used at all.
COPY --from=uv /uv /usr/local/bin/uv

# Dependencies first (cached layer): only changes when pyproject/uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# App code + UI + observability launcher (its tests are not shipped).
COPY streaming_core ./streaming_core
COPY voice_assistant ./voice_assistant
COPY mock_gateway ./mock_gateway
COPY guardrails ./guardrails
COPY static ./static
COPY server.py ./
COPY observability ./observability
RUN rm -rf ./observability/tests

# Run as a non-root user.
# uv is only needed at build time; removing it leaves one less tool in the
# running container for an attacker to use (CR-11: minimal image).
RUN rm /usr/local/bin/uv \
    && useradd --create-home --uid 10001 app && chown -R app /app
USER app

# Config comes from Cloud Run env vars; Vertex AI, Vertex AI Search, GCS and
# Cloud Telemetry (OTel) auth via the service account (ADC). Never bake .env into the image.
# These are safe defaults; ./deploy.sh sets the same variables from .env.
#   ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false: ADK defaults it to TRUE when unset,
#     which would put caller speech (PII) into trace spans.
#   OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT: same rule for
#     the OpenTelemetry GenAI instrumentation.
#   LOG_FORMAT=json: one JSON line per log, which Cloud Logging reads as
#     structured fields (severity, trace, spanId, sourceLocation).
#   OTEL_TRACES_SAMPLER: trace every call by default ([Observability #13], see .env.example).
ENV HOST=0.0.0.0 \
    PORT=8080 \
    OTEL_TO_CLOUD=true \
    OTEL_SERVICE_NAME=maxis-voice-agent \
    ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false \
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT \
    LOG_FORMAT=json \
    LOG_LEVEL=INFO \
    OTEL_TRACES_SAMPLER=parentbased_always_on
EXPOSE 8080

# Exec form, venv python directly (faster cold start than `uv run`): Python is
# PID 1 and receives Cloud Run's SIGTERM itself, so observability/shutdown.py
# can close calls cleanly within the 10 s grace period.
CMD ["/app/.venv/bin/python", "-m", "observability"]
