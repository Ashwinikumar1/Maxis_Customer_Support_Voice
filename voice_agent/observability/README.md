# Observability add-on

Closes the tracing and logging gaps in the voice agent. It is part of the main image:
`../Dockerfile` copies this package and starts the server through it. No new dependencies.

## Run

```bash
cd voice_agent
uv run python -m observability        # instead of: uv run python server.py
```

Deploy: just `./deploy.sh`. It builds `../Dockerfile` (which includes this add-on) and sets
all of its env vars in one step. There is no separate observability deploy any more.

## What it adds

| Gap | What changes | Where |
|---|---|---|
| #1 logs not linked to traces | One JSON line per log on stdout with `severity`, `message`, `time`, `logging.googleapis.com/trace`, `/spanId`, `/trace_sampled`, `/sourceLocation`, plus `session_id`. Tracebacks stay one entry and reach Error Reporting. Python warnings are JSON too. `LOG_LEVEL` works. `websockets` INFO noise is muted. | `logs.py` |
| #2 failed calls look OK in Cloud Trace | Any ERROR log inside a span marks that span ERROR and records the exception. The server's existing `logger.exception("Session ... failed")` therefore fails the `voice_session` span. | `logs.py` |
| #3 FAQ tool failures invisible | `vertex_ai_search.search` CLIENT span (status, HTTP code, result count) and `voice.faq_search.duration` metric. The failure is logged once, by the tool's own `except`. Query text is never recorded. The caller still hears the same fallback message. | `faq_tracing.py` |
| #4 SIGTERM loses data | SIGTERM logs the shutdown, closes open calls with WebSocket code 1001 ("going away"), and stops the server through asyncio's own Ctrl-C handler (never by raising into running code, which hung ~1 shutdown in 20). Each call's spans end, its transcript uploads, and it is recorded as `close_reason=server_shutdown`. Telemetry is force-flushed at 7 s (watchdog) and again on exit. Measured on Python 3.12: 40/40 clean exits, slowest 0.51 s. | `shutdown.py`, `ws_hooks.py` |
| #3 for roaming: gateway failures invisible | `agent_gateway.request` CLIENT span per roaming-tool call (operation = OpenAPI operationId, HTTP status, the gateway's `next_step`, or `violation_code`/`violation_action`). ERROR on network errors, 5xx, 401/403 and `BACKEND_UNAVAILABLE`. A refusal by Maxis's rules (e.g. `NOT_A_MAXIS_NUMBER`) is recorded but not an error. Plus the `voice.gateway.duration` metric (ms, by operation and outcome). The failure is logged once, by `gateway_client`, with the traceback, which also marks the tool's span ERROR. The caller's number and the arguments are never recorded. `httpx` INFO lines (one per call, with the URL) are muted. | `gateway_tracing.py`, `voice_assistant/gateway_client.py` |
| Agents and hand-over | One log line when the active agent changes and one per `end_session` (escalated, intent, use case). The call span gets `voice.call_outcome` (contained / escalated / abandoned), `voice.last_agent`, `voice.handover.*` and `voice.verification_level`. The `voice.calls` metric is counted by outcome, intent, routing mode and last agent (containment and handoff rate). `close_reason` adds `agent_end_session`, `silence_timeout` and `time_limit`. | `server.py` |
| Security events | Guardrail events (`guardrails/events.py`) are trace-linked JSON lines (`logging.googleapis.com/trace`, `spanId`, `session_id`) and also span events `security_event.<name>` on the active span. A refused tool call sets `guardrails.refused_reason` on its span. | `guardrails/events.py` |
| #7 PII default, standard ids | `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false` and `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT` by default, baked into the image too. Child spans get `session.id` and `gen_ai.conversation.id`. That value is the call's hashed `session_ref` (guardrails LM-07), the same as `session_id` on every log line. The transcript holds the raw session ID, `session_ref` and `trace_id`. | `settings.py`, `spans.py`, `server.py` |
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
| `OBS_GATEWAY_TRACING` | Agent gateway (roaming tools) span and metric |
| `OBS_SIGTERM` | Graceful SIGTERM handling |
| `OBS_SHUTDOWN_FLUSH_AFTER_S` | Watchdog flush delay after SIGTERM, default `7` |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | Set to `gen_ai_latest_experimental` in `.env` to opt in (test in staging first) |

## How it attaches

`python -m observability` installs the hooks, then runs `server.py` with `runpy` as `__main__`.
The hooks wrap three module attributes before `server.py` uses them:
`google.adk.telemetry.setup.maybe_set_otel_providers`, `websockets.asyncio.server.serve`,
`voice_assistant.faq_search_tool._search` and `voice_assistant.gateway_client._send`. If an upgrade renames one of them, that hook logs an
error and the server starts without it.

## Tests

```bash
cd voice_agent && uv run python -m unittest discover -s observability/tests -t .
```

57 tests, including the gateway and security-event tracing, three end-to-end roaming calls through the real
server (`test_roaming_end_to_end.py`: buy a pass, gateway down, purchase refused; scripted model, everything
else real), and two end-to-end runs of the real `server.py`: a failing call (span marked
ERROR, logs carry `session_id` and trace), a cross-site 403, and SIGTERM during an in-progress
call (transcript saved, root span exported, exit in under 10 s).
