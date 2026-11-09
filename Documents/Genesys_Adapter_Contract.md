# Genesys adapter contract (ces-genesys-adapter ↔ our server)

What the deployed `ces-genesys-adapter` sends us, what we must send back, and what reaches
Genesys. Read from the adapter source, Google's
[ces-genesys-adapter](https://github.com/GoogleCloudPlatform/ces-genesys-adapter) at `HEAD 8864bd1`
(27 Oct 2026), plus the live GECX app export (`voicebot-agent`).

> [!WARNING]
> **Not yet checked against the deployed service.** `ces-genesys-adapter-stg`
> (`mxs-maxisapp-prd`, `us-central1`) may run an older build. Check its image, env and one real
> GECX call's logs ([section 8](#8-to-verify-on-the-deployed-service)) before relying on the details
> below.

## 1. One call, end to end

```
Genesys                     adapter (Cloud Run)                       our server /ces/<location>
  │ WS + X-API-KEY + Signature │                                          │
  │──open {conversationId,────▶│ checks key+HMAC; reads inputVariables    │
  │   inputVariables, media}   │──WS  Authorization: Bearer <token> ─────▶│ 401 if token wrong
  │                            │──{"config":{session, MULAW 8k in/out}}──▶│
  │                            │──{"realtimeInput":{"variables":{…}}}────▶│
  │                            │──{"realtimeInput":{"text":…}} or ───────▶│ kickstart → bot greets
  │                            │   {"realtimeInput":{"event":{"event":"session_start"}}}
  │◀──────────── opened ───────│                                          │
  │══ binary PCMU 8k ═════════▶│──{"realtimeInput":{"audio":b64}}────────▶│
  │                            │◀─{"sessionOutput":{"audio":b64}}─────────│ bot speech
  │◀═ paced PCMU (≥280 ms) ════│                                          │
  │                            │◀─{"interruptionSignal":{}}───────────────│ barge-in: adapter drops queued audio
  │──dtmf {digit}─────────────▶│──{"realtimeInput":{"dtmf":"5"}}─────────▶│
  │                            │◀─{"endSession":{"metadata":{…,"params":{…}}}}  then close 1000
  │◀─ disconnect(completed,    │ plays out queued audio, then             │
  │    outputVariables=params) │                                          │
```

Caller hangs up first: Genesys `close` → adapter sends
`{"realtimeInput":{"event":{"event":"sys.remote-call-disconnected"}}}` + `{"clientHalfClose":true}`,
waits ≤ 5 s for our `endSession`, then closes.

## 2. Genesys → adapter: input variables (Architect "Call Audio Connector")

| Variable | Used by | Effect |
|---|---|---|
| `_agent_id` | adapter (**required** unless `_deployment_id`) | `projects/<p>/locations/<loc>/apps/<app>`. `<loc>` is appended to the CES URL; the full value prefixes our session name |
| `_deployment_id` | adapter | `…/apps/<app>/deployments/<d>` (exactly 8 parts); agent id is derived from it; sent as `config.deployment` |
| `_initial_message` | adapter | JSON-decoded; sent as `realtimeInput.text` **instead of** `session_start` |
| `_session_id` | adapter | Overrides the session id (default: Genesys `conversationId`) |
| anything else, e.g. `caller_id` | **forwarded to us** | in `realtimeInput.variables`, plus `adapterSessionId` (adapter's own UUID) |

- Variables starting with `_` are **never** forwarded.
- The live GECX app declares `caller_id`: "Calling number. **Set by Genesys**". So today's
  Architect flow most likely passes `caller_id`. Our bridge reads, in order: `msisdn`,
  `caller_id`, `ani`, `telephony-caller-id` (digits only, 8–15). None of them → `anonymous`.
- `conversationId = 00000000-…` is a Genesys connection probe: the adapter doesn't call us.
- Only `PCMU` 8000 Hz media is accepted.

## 3. Adapter → our server (all handled by `channels/ces_bridge.py`)

| Message | Our handling |
|---|---|
| headers `Authorization: Bearer <token>`, `X-Goog-User-Project` | token checked before the upgrade (401); project ignored |
| `config.session` = `<_agent_id>/sessions/<conversationId>` | tail → ADK `session_id`, `X-Conversation-Id` |
| `config.deployment`, `config.historicalContexts` | ignored (`historicalContexts` is only used when the adapter reconnects after a `goAway`, which we never send) |
| `realtimeInput.variables` | cleaned → session state `channel_vars`; MSISDN as above |
| `realtimeInput.text` (kickstart) / `event.session_start` | starts the call; text or `CES_BRIDGE_KICKSTART_TEXT` is the first user turn |
| `realtimeInput.audio` (MULAW 8k, any frame size) | → PCM16 16k → Gemini Live |
| `realtimeInput.dtmf` | → text turn "[The caller pressed 5 …]" |
| event `sys.remote-call-disconnected` / `clientHalfClose` | caller gone → `endSession` (no params) → close 1000 |

## 4. Our server → adapter, and what the adapter does with it

| We send | Adapter does |
|---|---|
| `{"sessionOutput":{"audio":b64 MULAW 8k}}` | queues; pacer sends to Genesys (primes 500 ms, then real time) |
| `{"interruptionSignal":{}}` | clears its queue and pacer buffer (barge-in) |
| `{"endSession":{"metadata":{"session_escalated","reason","params"}}}` then close 1000 | waits ≤ 5 s for more audio, drains it, sends Genesys `disconnect` **reason `completed`** with `outputVariables = metadata.params` (only `params`!) |
| close 1011 / 1008 / other, no `endSession` | `disconnect` **reason `error`**, so Architect takes its failure path |
| close 1011 with reason containing `resource_exhausted` | `disconnect normal`, `outputVariables {"SYS_ERROR":"CES_RESOURCE_EXHAUSTED"}` |
| close 1007 | `disconnect normal`, `outputVariables {"SYS_ERROR":"CES_UNSUPPORTED_PAYLOAD"}` |
| `sessionOutput.text`, `recognitionResult`, anything else | logged / ignored |

> [!IMPORTANT]
> Close 1000 **without** `endSession` leaves the Genesys leg open, because the adapter sends no
> `disconnect`. Our bridge always sends `endSession` before a normal close. Keep it that way.

## 5. Output variables (what Architect receives)

Only `endSession.metadata.params` reaches Genesys. Non-string values are JSON-encoded by the
adapter; we send flat strings.

| Live GECX bot (`end_session` params, from its instructions) | Our bridge sends |
|---|---|
| `intent` (fibre_troubleshooting, roaming, faq, other) | — (not yet) |
| `summary` (what happened + what's still needed) | `summary` (= `customer_request`) |
| `language` (`en` / `ms`) | — (not yet) |
| `use_case`, `destination`, pass name | `use_case`, `destination`, `pass_name` |
| `customer_request` | `customer_request` |
| `session_escalated` is in `metadata`, **not** `params` → doesn't reach Genesys | `session_escalated` (`"true"`/`"false"`) in params as well |
| — | `outcome` (`handoff` / `end`), `handoff_reason`, `end_reason` |

> [!WARNING]
> **Open question for the Maxis Genesys owner:** which outputVariables does the current Architect
> flow branch on after "Call Audio Connector"? (Escalation vs end, queue by `intent` / `language`?)
> Our names were chosen before we saw the GECX contract. If Architect uses `intent` and `language`,
> add them to `escalateToAgent` (`voice_assistant/shared/tools.py`) and `output_variables()`.

## 6. Auth between adapter and us

- `AUTH_TOKEN_SECRET_PATH` set → the adapter reads Secret Manager **`projects/<p>/secrets/<name>`**
  (full resource name; `/versions/latest` is added). The payload is
  `{"access_token": "…", "expiry": <epoch ms>}` and is re-read when `expiry` passes. Our server
  mounts the same secret as `CES_BRIDGE_TOKEN`.
- Not set → the adapter sends its **service account's Google OAuth access token** (ADC). That's
  what the GECX adapter does today, since real CES needs it.
- If the token can't be fetched, the adapter rejects Genesys with WS 1013, and its health checks
  fail.

Option (not built): accept the adapter's Google access token instead of a shared secret, verifying
via Google's tokeninfo that it belongs to the adapter's service account. Then the second adapter
service would only need `CES_WS_URL` changed and no new secret.

## 7. Changes needed on the adapter side (unchanged by this review)

1. `src/ces_ws.py`: `_BASE_WS_URL` is hard-coded to `wss://ces.googleapis.com/…/locations/`
   even at `HEAD`, so make it `os.getenv("CES_WS_URL", <current value>)`.
2. Deploy a **second** service from that image: `CES_WS_URL=wss://<our host>/ces/`,
   `AUTH_TOKEN_SECRET_PATH=projects/<p>/secrets/<name>`, its own `GENESYS_API_KEY` /
   `GENESYS_CLIENT_SECRET`. Repointing `ces-genesys-adapter-stg` would move the GECX bot.
3. A Genesys Audio Connector integration + staging Architect flow pointing at the new service,
   with `_agent_id = projects/<p>/locations/us/apps/adk` (only `us` matters) and the caller's
   number in `caller_id` (or `msisdn`).

## 8. To verify on the deployed service

Needs `gcloud auth login` (credentials have expired).

```bash
P=mxs-maxisapp-prd; R=us-central1; S=ces-genesys-adapter-stg
gcloud run services describe $S --project $P --region $R --format=yaml   # image, env, secrets, SA, ingress
gcloud run revisions list --service $S --project $P --region $R --limit 3
# one real GECX call: variables in, outputVariables out (values are redacted unless LOG_UNREDACTED_DATA)
gcloud logging read 'resource.labels.service_name="'$S'" AND (jsonPayload.log_type="ces_send_variables" OR jsonPayload.log_type="genesys_disconnect_start" OR jsonPayload.log_type="ces_recv_endsession")' \
  --project $P --limit 20 --freshness 7d --format=json
```

Check: the build matches this contract (`_session_id`, `goAway`, the `SYS_ERROR` mapping are recent);
whether `AUTH_TOKEN_SECRET_PATH` is set; `DISCONNECT_EVENT_NAME` (default
`sys.remote-call-disconnected`); the real `inputVariables` keys; and the `outputVariables` the
GECX bot produces.
