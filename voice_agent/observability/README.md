# Observability add-on

Closes the tracing and logging gaps that can be fixed **without editing any existing file**.
`server.py`, `streaming_core/`, `voice_assistant/`, `Dockerfile`, `deploy.sh`, `pyproject.toml`
and `.env.example` are untouched; no new dependencies.

## Run

```bash
cd voice_agent
uv run python -m observability        # instead of: uv run python server.py
```

Deploy (after `./deploy.sh` has created the service at least once):

```bash
./observability/deploy.sh             # builds observability/Dockerfile, rolls it onto the same service
```

Running `./deploy.sh` again puts the plain image back; run `./observability/deploy.sh` after it.

## What it adds

| Gap | What changes | Where |
|---|---|---|
| #1 logs not linked to traces | One JSON line per log on stdout with `severity`, `message`, `time`, `logging.googleapis.com/trace`, `/spanId`, `/trace_sampled`, `/sourceLocation`, plus `session_id`. Tracebacks stay one entry and reach Error Reporting. Python warnings are JSON too. `LOG_LEVEL` works. `websockets` INFO noise is muted. | `logs.py` |
| #2 failed calls look OK in Cloud Trace | Any ERROR log inside a span marks that span ERROR and records the exception. The server's existing `logger.exception("Session ... failed")` therefore fails the `voice_session` span. | `logs.py` |
| #3 FAQ tool failures invisible | `vertex_ai_search.search` CLIENT span (status, HTTP code, result count) and `voice.faq_search.duration` metric. The failure is logged once, by the tool's own `except`. Query text is never recorded. The caller still hears the same fallback message. | `faq_tracing.py` |
| #4 SIGTERM loses data | SIGTERM logs the shutdown, closes open calls with WebSocket code 1001 ("going away"), and stops the server through asyncio's own Ctrl-C handler (never by raising into running code, which hung ~1 shutdown in 20). Each call's spans end, its transcript uploads, and it is recorded as `close_reason=server_shutdown`. Telemetry is force-flushed at 7 s (watchdog) and again on exit. Measured on Python 3.12: 40/40 clean exits, slowest 0.51 s. | `shutdown.py`, `ws_hooks.py` |
| #7 PII default, standard ids | `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false` and `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT` by default, baked into the image too. Child spans get `session.id` and `gen_ai.conversation.id`. | `settings.py`, `spans.py` |
| #9 session metrics, security logs | `voice.sessions.active`, `voice.session.duration`, `voice.sessions` (by outcome). WARNING log + `voice.ws.rejected` counter for every cross-site WebSocket 403. | `spans.py`, `ws_hooks.py` |
| P2 version | `service.version=<git sha>` on every span, metric and log resource. | `deploy.sh` |

## Switches (all default on)

| Variable | Effect |
|---|---|
| `LOG_FORMAT` | `json` or `text` (default: `json` on Cloud Run, `text` locally) |
| `LOG_LEVEL` | Root log level, default `INFO` |
| `OBS_SESSION_SPANS` | Session metrics and `session.id` propagation |
| `OBS_WS_SECURITY_LOGS` | 403 logging |
| `OBS_FAQ_TRACING` | FAQ search span, log and metric |
| `OBS_SIGTERM` | Graceful SIGTERM handling |
| `OBS_SHUTDOWN_FLUSH_AFTER_S` | Watchdog flush delay after SIGTERM, default `7` |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | Set to `gen_ai_latest_experimental` in `.env` to opt in (test in staging first) |

## How it attaches

`python -m observability` installs the hooks, then runs `server.py` with `runpy` as `__main__`.
The hooks wrap three module attributes before `server.py` uses them:
`google.adk.telemetry.setup.maybe_set_otel_providers`, `websockets.asyncio.server.serve` and
`voice_assistant.faq_search_tool._search`. If an upgrade renames one of them, that hook logs an
error and the server starts without it.

## Tests

```bash
cd voice_agent && uv run python -m unittest discover -s observability/tests -t .
```

23 tests, including two end-to-end runs of the real `server.py`: a failing call (span marked
ERROR, logs carry `session_id` and trace), a cross-site 403, and SIGTERM during an in-progress
call (transcript saved, root span exported, exit in under 10 s).
