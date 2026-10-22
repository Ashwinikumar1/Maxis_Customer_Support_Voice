# Maxis voice agent WITH the observability add-on.
# Same image as ../Dockerfile (which is left untouched) plus the observability/
# package, started through its launcher. Build from voice_agent/:
#   docker build -f observability/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
RUN pip install --no-cache-dir uv

# Dependencies: the existing lockfile, no new packages.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY streaming_core ./streaming_core
COPY voice_assistant ./voice_assistant
COPY static ./static
COPY server.py ./
COPY observability ./observability
RUN rm -rf ./observability/tests ./observability/Dockerfile ./observability/*.sh ./observability/*.yaml

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

# OTel and logging defaults. ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS is baked in
# because ADK defaults it to TRUE (caller speech in spans) when unset.
ENV HOST=0.0.0.0 \
    PORT=8080 \
    OTEL_TO_CLOUD=true \
    OTEL_SERVICE_NAME=maxis-voice-agent \
    ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false \
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT \
    LOG_FORMAT=json \
    LOG_LEVEL=INFO
EXPOSE 8080

# Exec form: Python is PID 1 and receives Cloud Run's SIGTERM directly.
CMD ["/app/.venv/bin/python", "-m", "observability"]
