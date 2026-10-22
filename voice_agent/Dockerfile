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
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
RUN pip install --no-cache-dir uv

# Dependencies first (cached layer): only changes when pyproject/uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# App code + UI + observability launcher (its tests are not shipped).
COPY streaming_core ./streaming_core
COPY voice_assistant ./voice_assistant
COPY static ./static
COPY server.py ./
COPY observability ./observability
RUN rm -rf ./observability/tests

# Run as a non-root user.
RUN useradd --create-home --uid 10001 app && chown -R app /app
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
