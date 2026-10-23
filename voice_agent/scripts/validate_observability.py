"""Checks that every logging and tracing change is live on the deployed service.

WHAT IT DOES
  1. Settings: reads the Cloud Run service and IAM, and checks the env vars,
     call settings and service-account roles that the changes rely on.
  2. Test call: makes ONE short call to the service, built to trigger the rare
     cases on purpose: no init message (init timeout), a bad JSON message and a
     bad audio frame (dropped-input counters), then one typed FAQ question.
     Also tries one connection from a foreign website (must get 403).
  3. Evidence: finds that call in Cloud Logging, Cloud Trace, Cloud Monitoring
     and the transcript bucket, and checks every field the changes added.
  4. History: start-up line of the live revision, shutdown lines from earlier
     deploys, and that no session-resumption handle was ever logged in clear.

Every check prints PASS / FAIL / WARN / INFO with the reason. Exit code 1 if
anything FAILs. Nothing is changed in the project; the only cost is the one
short Gemini call (about 1,000 tokens).

NEEDS: `gcloud auth application-default login` (an account that can read Cloud
Run, IAM, Logging, Trace, Monitoring and the bucket) and voice_agent/.env.

USAGE (from voice_agent/):
  uv run python scripts/validate_observability.py
  uv run python scripts/validate_observability.py --no-call   # settings + history only
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import google.auth
from dotenv import dotenv_values
from google.auth.transport.requests import AuthorizedSession
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

QUESTION = "Can I schedule my roaming pass before I fly?"
PRIVACY_PROBE = "schedule my roaming pass"   # must never appear in a span attribute
EXPECTED_ROLES = ["roles/aiplatform.user", "roles/discoveryengine.viewer",
                  "roles/serviceusage.serviceUsageConsumer", "roles/telemetry.tracesWriter",
                  "roles/monitoring.metricWriter", "roles/logging.logWriter"]
OLD_ROLES = ["roles/cloudtrace.agent", "roles/telemetry.logsWriter", "roles/telemetry.metricsWriter"]
METRICS = ["voice.sessions", "voice.sessions.active", "voice.session.duration", "voice.time_to_first_audio",
           "voice.model_turns", "voice.call.tokens", "voice.faq_search.duration", "voice.ws.rejected",
           "voice.calls"]
# Exported only once a roaming tool has called the agent gateway (not by this script's test call),
# so a missing one is a warning, not a failure.
OPTIONAL_METRICS = ["voice.gateway.duration"]

RESULTS = []


def report(status, check, detail=""):
  RESULTS.append(status)
  colour = {"PASS": "32", "FAIL": "31", "WARN": "33", "INFO": "36"}[status]
  print(f"  \033[{colour}m{status:4}\033[0m  {check}" + (f"  - {detail}" if detail else ""))


def check(condition, name, detail_ok="", detail_bad="", bad="FAIL"):
  report("PASS" if condition else bad, name, detail_ok if condition else detail_bad)
  return condition


class Gcp:
  """Thin REST client (google-auth only; no extra packages)."""

  def __init__(self, project):
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    self.http = AuthorizedSession(creds)
    self.project = project

  def call(self, method, url, **kw):
    resp = self.http.request(method, url, headers={"X-Goog-User-Project": self.project}, timeout=30, **kw)
    if resp.status_code >= 400:
      raise RuntimeError(f"HTTP {resp.status_code} from {url.split('?')[0]}: {resp.text[:300]}")
    return resp.json() if resp.content and "json" in resp.headers.get("content-type", "") else resp.content

  def logs(self, filter_, since, limit=50):
    body = {"resourceNames": [f"projects/{self.project}"], "orderBy": "timestamp desc", "pageSize": limit,
            "filter": f'{filter_}\ntimestamp>="{since.isoformat()}"'}
    return self.call("POST", "https://logging.googleapis.com/v2/entries:list", json=body).get("entries", [])


def msg(entry):
  return (entry.get("jsonPayload") or {}).get("message") or entry.get("textPayload") or ""


def poll(fn, seconds, every=10):
  """Calls fn() until it returns something truthy or time runs out."""
  deadline = time.monotonic() + seconds
  while True:
    value = fn()
    if value or time.monotonic() > deadline:
      return value
    time.sleep(every)


# --------------------------------------------------------------------------- 1. settings
def check_settings(gcp, env, region, service):
  print("\n1. Service settings (Cloud Run + IAM)")
  svc = gcp.call("GET", f"https://run.googleapis.com/v2/projects/{gcp.project}/locations/{region}/services/{service}")
  tmpl = svc["template"]
  container = tmpl["containers"][0]
  run_env = {e["name"]: e.get("value", "") for e in container.get("env", [])}
  revision = svc.get("latestReadyRevision", "").split("/")[-1]
  report("INFO", "Live revision", revision)

  for name, want in [("OTEL_TO_CLOUD", "true"), ("LOG_FORMAT", "json"), ("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "false"),
                     ("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "NO_CONTENT")]:
    check(run_env.get(name, "").lower() == want.lower(), f"Env {name}={want}", detail_bad=f"is {run_env.get(name)!r}")
  check(bool(run_env.get("LOG_LEVEL")), "Env LOG_LEVEL set", run_env.get("LOG_LEVEL", ""), "missing")
  check(run_env.get("OTEL_TRACES_SAMPLER", "").startswith("parentbased"), "Env OTEL_TRACES_SAMPLER (parent-based)",
        run_env.get("OTEL_TRACES_SAMPLER", ""), f"is {run_env.get('OTEL_TRACES_SAMPLER')!r}")
  version = (re.search(r"service\.version=([^,]+)", run_env.get("OTEL_RESOURCE_ATTRIBUTES", "")) or [None, None])[1]
  check(bool(version), "Env OTEL_RESOURCE_ATTRIBUTES has service.version", version or "", "missing")
  check(run_env.get("FAQ_SEARCH_ENGINE_ID") == env.get("FAQ_SEARCH_ENGINE_ID"), "Env FAQ_SEARCH_ENGINE_ID matches .env",
        run_env.get("FAQ_SEARCH_ENGINE_ID", ""), f"service has {run_env.get('FAQ_SEARCH_ENGINE_ID')!r}, .env has "
        f"{env.get('FAQ_SEARCH_ENGINE_ID')!r} - run ./deploy.sh", bad="WARN")
  report("INFO", "Gemini location (GOOGLE_CLOUD_LOCATION)", run_env.get("GOOGLE_CLOUD_LOCATION", "?"))

  check(tmpl.get("timeout") == "3600s", "Request timeout 3600 s (long calls)", detail_bad=f"is {tmpl.get('timeout')}")
  check(tmpl.get("sessionAffinity") is True, "Session affinity on")
  # The v2 API omits cpuIdle when it is false, so read Cloud Run's own setting
  # (the run.googleapis.com/cpu-throttling annotation that --no-cpu-throttling sets).
  try:
    v1 = gcp.call("GET", f"https://{region}-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/"
                         f"{gcp.project}/services/{service}")
    throttling = v1["spec"]["template"]["metadata"].get("annotations", {}).get("run.googleapis.com/cpu-throttling")
  except (RuntimeError, KeyError):
    throttling = None
  if throttling is None:
    throttling = "true" if container.get("resources", {}).get("cpuIdle") else "unknown"
  check(throttling == "false", "CPU always allocated (--no-cpu-throttling)",
        detail_bad=("CPU is cut between requests, so telemetry export can stall - run ./deploy.sh" if throttling == "true"
                    else "could not read the setting - check Cloud Run > Edit > CPU is always allocated"),
        bad="FAIL" if throttling == "true" else "WARN")

  sa = tmpl.get("serviceAccount", "")
  policy = gcp.call("POST", f"https://cloudresourcemanager.googleapis.com/v1/projects/{gcp.project}:getIamPolicy", json={})
  roles = {b["role"] for b in policy.get("bindings", []) if f"serviceAccount:{sa}" in b.get("members", [])}
  missing = [r for r in EXPECTED_ROLES if r not in roles]
  check(not missing, f"Service account roles ({sa.split('@')[0]})", "all 6 present", f"missing {missing}")
  extra = [r for r in OLD_ROLES if r in roles]
  check(not extra, "Old duplicate roles removed", detail_bad=f"still granted {extra} (runbook: After the tests, step 2)", bad="WARN")
  return run_env, revision, version


# --------------------------------------------------------------------------- 2. test call
def _who_refused(exc):
  """The app answers 403 with the body 'Forbidden'; Cloud Run's own 403 is an HTML page."""
  body = (exc.response.body or b"").decode("utf-8", "replace").strip()
  return "app" if body == "Forbidden" else "cloud_run", body[:120].replace("\n", " ")


async def make_test_call(base_url):
  """Returns True if the test call connected (so its evidence can be looked up)."""
  print("\n2. Test call")
  scheme, host = base_url.split("://", 1)
  ws_url = f"{'wss' if scheme == 'https' else 'ws'}://{host}/ws"
  report("INFO", "Calling", f"{ws_url} with Origin {base_url} (from the service's ALLOWED_ORIGINS)")
  try:  # [security] a connection from another website must be refused BY THE APP
    async with connect(ws_url, origin="https://evil.example", open_timeout=15):
      report("FAIL", "Foreign-origin connection refused", "it was ACCEPTED")
  except InvalidStatus as exc:
    who, body = _who_refused(exc)
    check(exc.response.status_code == 403 and who == "app", "Foreign-origin connection refused by the app (HTTP 403)",
          detail_bad=f"HTTP {exc.response.status_code} from {who}: {body}")

  got = {"audio_after_question": False, "types": set()}
  try:
    ws = await connect(ws_url, origin=base_url, open_timeout=20, max_size=2**22)
  except InvalidStatus as exc:
    who, body = _who_refused(exc)
    if who == "app":
      hint = ("the app refused our Origin: ALLOWED_ORIGINS does not match the URL - compare it with the URL "
              "./deploy.sh printed")
    else:
      hint = ("Cloud Run refused the request before the app saw it - usually public access (allUsers -> "
              "roles/run.invoker) is not granted, e.g. blocked by an organisation policy, or ingress is restricted")
    report("FAIL", "Test call connected", f"HTTP {exc.response.status_code}: {hint}. Body: {body}")
    return False
  except OSError as exc:
    report("FAIL", "Test call connected", f"{type(exc).__name__}: {exc}")
    return False
  report("PASS", "Test call connected")
  try:
    async def reader():
      # gemini-3.8-live sends turn_complete twice when it uses a tool: first after
      # the filler ("let me check", state IN_PROGRESS), then after the spoken
      # answer (state IDLE). Only IDLE means the answer is finished, and only
      # audio after the tool result is the answer itself.
      async for raw in ws:
        message = json.loads(raw)
        kind = message.get("type")
        got["types"].add(kind)
        if not got.get("asked"):
          continue
        if kind == "tool_result":
          got["tool_used"] = True
        elif kind == "audio" and (got.get("tool_used") or not got.get("tool_pending")):
          got["audio_after_question"] = True
        elif kind == "tool_call":
          got["tool_pending"], got["audio_after_question"] = True, False  # filler audio isn't the answer
        elif kind == "turn_complete" and message.get("state", "IDLE") == "IDLE" and got.get("audio_after_question"):
          got["answer_done"] = True  # (an IDLE before any answer audio is the greeting ending)
    task = asyncio.create_task(reader())
    await asyncio.sleep(6.5)                                          # no init -> init-timeout log
    await ws.send("this is not json")                                 # -> invalid_message
    await ws.send(json.dumps({"type": "audio", "data": "!!!"}))       # -> invalid_audio
    await asyncio.sleep(3)                                            # let the greeting start
    got["asked"] = True
    await ws.send(json.dumps({"type": "text", "data": QUESTION}))
    # Wait for the answer to FINISH (turn_complete, state IDLE) before hanging up:
    # Gemini sends its token report at the end of a turn, so hanging up mid-answer
    # leaves the call with tokens=n/a (seen on the first real run).
    deadline = time.monotonic() + 45                                  # FAQ search + spoken answer
    while not got.get("answer_done") and time.monotonic() < deadline and not task.done():
      await asyncio.sleep(0.5)
    await asyncio.sleep(2)                                            # let the usage report arrive
    await ws.send(json.dumps({"type": "end"}))
    await asyncio.sleep(1)
    task.cancel()
  finally:
    await ws.close()
  check(got["audio_after_question"], "Agent spoke its answer" + (" (audio after the FAQ result)" if got.get("tool_used") else ""),
        detail_bad=f"no answer audio within 45 s (received: {sorted(t for t in got['types'] if t)})")
  check(bool(got.get("answer_done")), "Answer finished before hanging up (turn_complete, IDLE)", bad="WARN",
        detail_bad="no turn_complete within 45 s - token checks below may show n/a")
  return True


# --------------------------------------------------------------------------- 3a. logs
def check_logs(gcp, service, since):
  print("\n3a. Logs (Cloud Logging)")
  base = f'resource.type="cloud_run_revision"\nresource.labels.service_name="{service}"'
  closed = poll(lambda: gcp.logs(base + '\njsonPayload.message=~"closed .reason=.*invalid_audio=1, invalid_messages=1"',
                                 since, 5), 120)
  if not check(bool(closed), "Test call's closing line found", detail_bad="not found within 2 min - did the call connect?"):
    return None, None
  entry = closed[0]
  text, payload = msg(entry), entry.get("jsonPayload", {})
  sid = payload.get("session_id", "")
  report("INFO", "Test session", sid)
  check(entry.get("severity") == "INFO", "Severity set (INFO, not DEFAULT)", detail_bad=str(entry.get("severity")))
  check("jsonPayload" in entry, "Structured JSON entry (jsonPayload)")
  trace = entry.get("trace", "")
  check(trace.startswith(f"projects/{gcp.project}/traces/"), "Linked to its trace (trace field)", trace, "missing")
  check(bool(entry.get("spanId")), "Span ID on the entry (spanId)", entry.get("spanId", ""), "missing")
  check("traceSampled" in entry and "logging.googleapis.com/trace_sampled" not in payload,
        "traceSampled moved to the entry")
  loc = entry.get("sourceLocation", {})
  check(bool(loc.get("file") and loc.get("line")), "Source location (file, line, function)",
        f"{loc.get('file')}:{loc.get('line')} {loc.get('function')}", "missing")
  check(bool(sid), "session_id in jsonPayload")
  check("reason=client_end" in text, "Close reason recorded (client_end)", detail_bad=text[:160])
  tokens = re.search(r"tokens_in=(\S+), tokens_out=(\S+), tokens_total=(\S+)\)", text)
  check(bool(tokens) and tokens.group(3).isdigit(), "Token usage in the closing line",
        f"in={tokens.group(1)} out={tokens.group(2)} total={tokens.group(3)}" if tokens else "", text[-90:])
  check("invalid_audio=1, invalid_messages=1" in text, "Dropped-input counters (1 bad audio, 1 bad message)")

  session_lines = gcp.logs(base + f'\njsonPayload.session_id="{sid}"', since, 100)
  messages = [msg(e) for e in session_lines]
  check(any(f"Session {sid} started" in m for m in messages), "'Session ... started' line")
  check(sum("dropped" in m for m in messages) == 2, "One warning per kind of dropped input",
        detail_bad=f"{sum('dropped' in m for m in messages)} 'dropped' lines (expected 2)")
  check(any("Transcript saved to gs://" in m for m in messages), "Transcript upload logged",
        detail_bad="no 'Transcript saved to gs://' line (local fallback or GCS_BUCKET empty?)")
  check(not any("FAQ search failed" in m for m in messages), "No FAQ search failures in the test call",
        detail_bad="'FAQ search failed' - check FAQ_SEARCH_ENGINE_ID")
  check(all(e.get("severity") not in (None, "DEFAULT") for e in session_lines), "Every line of the call has a severity")
  check(not any(PRIVACY_PROBE in m.lower() for m in messages), "Caller's question not written to logs")

  check(bool(gcp.logs(base + '\njsonPayload.message=~"No init message within"', since, 5)), "Init timeout logged")
  check(bool(gcp.logs(base + '\njsonPayload.message=~"WebSocket rejected: origin https://evil.example"', since, 5)),
        "Rejected connection logged (security WARNING)")
  noise = gcp.logs(base + '\njsonPayload.logger=~"^websockets"\nseverity<WARNING', since, 5)
  check(not noise, "websockets INFO noise muted", detail_bad=f"{len(noise)}+ websockets lines")
  return sid, trace.split("/")[-1]


# --------------------------------------------------------------------------- 3b. trace
def check_trace(gcp, trace_id, sid, engine_id, version):
  print("\n3b. Trace (Cloud Trace)")

  def fetch():
    try:
      spans = gcp.call("GET", f"https://cloudtrace.googleapis.com/v1/projects/{gcp.project}/traces/{trace_id}").get("spans", [])
      names = {s.get("name") for s in spans}
      return spans if "voice_session" in names else None
    except RuntimeError:
      return None
  spans = poll(fetch, 120)
  if not check(bool(spans), "Trace found (voice_session span)", trace_id,
               "not found within 2 min (needs the Cloud Trace API enabled and read access)"):
    return
  by_name = {}
  for s in spans:
    by_name.setdefault(s.get("name"), []).append(s.get("labels", {}))
  if not any(by_name.get("voice_session", [{}])[0].values()):
    report("WARN", "Span attributes not returned by the Trace API", "check the remaining items in Trace Explorer")
    return
  call = by_name["voice_session"][0]
  check(call.get("session.id") == sid, "voice_session: session.id")
  check(call.get("voice.close_reason") == "client_end", "voice_session: voice.close_reason", call.get("voice.close_reason", ""))
  check(call.get("voice.dropped.invalid_audio") == "1" and call.get("voice.dropped.invalid_message") == "1",
        "voice_session: voice.dropped.* = 1 / 1")
  check(str(call.get("voice.tokens.total", "")).isdigit(), "voice_session: voice.tokens.total", call.get("voice.tokens.total", ""))

  turns = by_name.get("model_turn", [])
  check(bool(turns), "model_turn spans (one per agent reply)", f"{len(turns)} turns", "none in the trace")
  if turns:
    typed = [t for t in turns if t.get("voice.turn_trigger") == "user_text"]
    by_trigger = ", ".join(f"{k}={sum(t.get('voice.turn_trigger') == k for t in turns)}"
                           for k in sorted({t.get("voice.turn_trigger") for t in turns} - {None}))
    check(bool(typed), "model_turn: typed question has trigger user_text", by_trigger)
    check(any("voice.time_to_first_audio_ms" in t for t in typed), "model_turn: time to first audio on the answered turn",
          detail_bad="no voice.time_to_first_audio_ms on any user_text turn")
    check(not any(t.get("voice.time_to_first_audio_ms") for t in turns if t.get("voice.turn_trigger") == "model"),
          "model_turn: no time-to-first-audio on model-started turns (greeting)")
    check(all(t.get("gen_ai.agent.name") for t in turns), "model_turn: gen_ai.agent.name on every turn",
          detail_bad="empty on some turns - deploy commit 3d6f144 or newer")
    check(all(t.get("session.id") == sid for t in turns), "model_turn: session.id on every turn")
  search = by_name.get("vertex_ai_search.search", [])
  if check(bool(search), "FAQ search span (vertex_ai_search.search)", bad="WARN",
           detail_bad="the agent did not search in this call - re-run"):
    s = search[0]
    check(s.get("vertex_ai_search.engine_id") == engine_id, "FAQ span: engine_id", s.get("vertex_ai_search.engine_id", ""))
    check(str(s.get("vertex_ai_search.result_count", "")).isdigit(), "FAQ span: result_count",
          s.get("vertex_ai_search.result_count", ""))
    check(s.get("gen_ai.conversation.id") == sid, "FAQ span: gen_ai.conversation.id (session propagated)")
  adk = [l for n, ls in by_name.items() if n.startswith(("invoke_agent", "execute_tool", "call_llm")) for l in ls]
  if adk:
    check(all(l.get("session.id") == sid for l in adk), "ADK spans carry session.id", f"{len(adk)} spans")
  all_values = [str(v).lower() for ls in by_name.values() for l in ls for v in l.values()]
  check(not any(PRIVACY_PROBE in v for v in all_values), "No caller text in any span attribute (privacy)")
  versions = {v for ls in by_name.values() for l in ls for k, v in l.items() if k.endswith("service.version")}
  if versions:
    check(version in versions, "service.version on spans", ", ".join(sorted(versions)))
  else:
    report("INFO", "service.version", "not returned by the Trace API - check a span's Resource in Trace Explorer")
  report("INFO", "Not checkable by API", "the span link to the Cloud Run request (voice_session -> Metadata & Links)")


# --------------------------------------------------------------------------- 3c. metrics
def check_metrics(gcp):
  print("\n3c. Metrics (Cloud Monitoring; exported about once a minute)")
  url = (f"https://monitoring.googleapis.com/v3/projects/{gcp.project}/metricDescriptors?filter="
         + quote('metric.type = starts_with("prometheus.googleapis.com/voice")'))
  types = [d["type"] for d in gcp.call("GET", url).get("metricDescriptors", [])]

  # Map each metric to its Cloud Monitoring type. The exporter may keep the dots
  # or turn them into underscores and add a suffix (e.g. _total); longer names are
  # matched first so voice.sessions never claims voice.sessions.active.
  found_types, claimed = {}, set()
  for name in sorted(METRICS + OPTIONAL_METRICS, key=len, reverse=True):
    flat = name.replace(".", "_")
    for t in sorted(types):
      seg = t.split("/")[1]
      if t not in claimed and (seg in (name, flat) or seg.startswith((name + "_", flat + "_"))):
        found_types[name] = t
        claimed.add(t)
        break
  find = found_types.get
  for name in METRICS:
    found = find(name)
    check(bool(found), f"Metric {name}", found or "", "never exported (or still within the first minute)")
  for name in OPTIONAL_METRICS:
    found = find(name)
    check(bool(found), f"Metric {name}", found or "",
          "not exported yet: make one call that uses a roaming tool, then re-run", bad="WARN")

  def recent(metric_type):
    # RFC 3339 with "Z": a "+00:00" offset in a URL turns into a space and is rejected.
    fmt = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    end = datetime.now(timezone.utc)
    q = (f"https://monitoring.googleapis.com/v3/projects/{gcp.project}/timeSeries?filter="
         + quote(f'metric.type = "{metric_type}"') + f"&interval.startTime={fmt(end - timedelta(minutes=15))}"
         + f"&interval.endTime={fmt(end)}&view=HEADERS")
    try:
      return gcp.call("GET", q).get("timeSeries")
    except RuntimeError as exc:
      errors[metric_type] = str(exc)[:160]
      return None
  errors = {}
  for name in ("voice.sessions", "voice.ws.rejected"):
    metric_type = find(name)
    if metric_type:
      check(bool(poll(lambda: recent(metric_type), 150, 15)), f"{name}: data from the test call (last 15 min)",
            detail_bad=errors.get(metric_type) or "no recent points yet - re-run in a few minutes", bad="WARN")


# --------------------------------------------------------------------------- 3d. transcript
def check_transcript(gcp, bucket, prefix, sid, trace_id):
  print("\n3d. Transcript (Cloud Storage)")
  if not bucket:
    report("WARN", "GCS_BUCKET empty in .env", "transcripts are saved locally; skipped")
    return
  safe = re.sub(r"[^A-Za-z0-9_-]", "", sid)[:64]
  listing = gcp.call("GET", f"https://storage.googleapis.com/storage/v1/b/{bucket}/o?prefix="
                     + quote(f"{prefix.strip('/')}/conversation_{safe}_"))
  items = listing.get("items", [])
  if not check(bool(items), "Transcript file in the bucket", detail_bad=f"none under gs://{bucket}/{prefix}/"):
    return
  raw = gcp.call("GET", f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(items[0]['name'], safe='')}?alt=media")
  meta = json.loads(raw if isinstance(raw, (bytes, str)) else json.dumps(raw)).get("metadata", {})
  check(meta.get("trace_id") == trace_id, "metadata.trace_id matches the trace", meta.get("trace_id", ""))
  usage = meta.get("token_usage") or {}
  check(bool(usage.get("total")), "metadata.token_usage recorded", f"total={usage.get('total')}")


# --------------------------------------------------------------------------- 4. history
def check_history(gcp, service, revision):
  print("\n4. History (last 7 days)")
  since = datetime.now(timezone.utc) - timedelta(days=7)
  base = f'resource.type="cloud_run_revision"\nresource.labels.service_name="{service}"'
  startup = gcp.logs(base + f'\nresource.labels.revision_name="{revision}"\njsonPayload.message=~"Observability add-on installed"', since, 1)
  if check(bool(startup), "Add-on start-up line on the live revision", detail_bad=f"none for {revision}"):
    text = msg(startup[0])
    check("hooks=adk_hook,ws_hooks,faq_tracing,shutdown" in text, "All 4 hooks installed", text[text.find("hooks="):][:70])
  leaked = gcp.logs(base + '\n"new_handle="\nNOT "[redacted]"', since, 3)
  check(not leaked, "Resumption handle never logged in clear", detail_bad=f"{len(leaked)}+ lines with a real handle")
  sigterm = gcp.logs(base + '\njsonPayload.message=~"SIGTERM received"', since, 1)
  check(bool(sigterm), "Clean-shutdown log seen on a past deploy", bad="INFO",
        detail_bad="none yet - runbook test 3 (deploy during a call) shows it")
  shut = gcp.logs(base + '\njsonPayload.message=~"closed .reason=server_shutdown"', since, 1)
  check(bool(shut), "A call closed as server_shutdown during a deploy", bad="INFO",
        detail_bad="none yet - runbook test 3")


def main():
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--no-call", action="store_true", help="skip the test call (settings and history only)")
  parser.add_argument("--project", help="override GOOGLE_CLOUD_PROJECT from .env")
  args = parser.parse_args()
  # .env wins over the shell, exactly like ./deploy.sh (which sources .env), so
  # a GOOGLE_CLOUD_PROJECT left over in the terminal can't point us elsewhere.
  env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
  if not os.path.exists(env_file):
    sys.exit(f"Missing {os.path.normpath(env_file)} - run from voice_agent/ after creating .env")
  env = {**os.environ, **{k: v for k, v in dotenv_values(env_file).items() if v is not None}}
  if args.project:
    env["GOOGLE_CLOUD_PROJECT"] = args.project
  project, region, service = env["GOOGLE_CLOUD_PROJECT"], env["CLOUD_RUN_REGION"], env["CLOUD_RUN_SERVICE"]
  print(f"Validating {service} in {project} / {region}")
  gcp = Gcp(project)

  run_env, revision, version = check_settings(gcp, env, region, service)
  if not args.no_call:
    since = datetime.now(timezone.utc) - timedelta(seconds=5)
    base_url = run_env.get("ALLOWED_ORIGINS", "").split(",")[0]
    connected = asyncio.run(make_test_call(base_url))
    sid, trace_id = check_logs(gcp, service, since) if connected else (None, None)
    if sid:
      check_trace(gcp, trace_id, sid, run_env.get("FAQ_SEARCH_ENGINE_ID"), version)
      check_transcript(gcp, run_env.get("GCS_BUCKET"), run_env.get("GCS_LOG_PREFIX", "VoiceAgentLogs"), sid, trace_id)
    check_metrics(gcp)
  check_history(gcp, service, revision)

  counts = {s: RESULTS.count(s) for s in ("PASS", "FAIL", "WARN", "INFO")}
  print(f"\nSummary: {counts['PASS']} passed, {counts['FAIL']} failed, {counts['WARN']} warnings, {counts['INFO']} info")
  return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
  sys.exit(main())
