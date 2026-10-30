# Maxis voice agent: one container serves the browser UI (static/) and the
# WebSocket voice API (/ws) on the same port. Built for Cloud Run.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
RUN pip install --no-cache-dir uv

# Dependencies first (cached layer): only changes when pyproject/uv.lock change.
# Export locked versions + hashes rather than `uv sync --frozen` directly so builds
# succeed in Cloud Build even if uv.lock was generated against a corp mirror.
COPY pyproject.toml uv.lock ./
RUN uv venv && uv export --frozen --no-dev --no-emit-project | uv pip install --no-cache -r -

# App code + UI.
COPY streaming_core ./streaming_core
COPY voice_assistant ./voice_assistant
COPY static ./static
COPY server.py ./

# Run as a non-root user.
RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

# Config comes from Cloud Run env vars; Vertex AI, Vertex AI Search, GCS and
# Cloud Telemetry (OTel) auth via the service account (ADC). Never bake .env into the image.
ENV HOST=0.0.0.0 \
    PORT=8080 \
    OTEL_TO_CLOUD=true \
    OTEL_SERVICE_NAME=maxis-voice-agent
EXPOSE 8080

# Run the venv's python directly (faster cold start than `uv run`).
CMD ["/app/.venv/bin/python", "server.py"]
