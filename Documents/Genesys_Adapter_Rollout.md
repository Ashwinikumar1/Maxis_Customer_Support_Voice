# Rollout: second Genesys adapter → our ADK voice agent (auth option A)

Option A: a **shared token in Secret Manager**. The second adapter reads it through
`AUTH_TOKEN_SECRET_PATH` and sends it as `Authorization: Bearer <token>`. Our server mounts the
same secret as `CES_BRIDGE_TOKEN` and checks it. No code changes on our side.

> [!IMPORTANT]
> Everything below runs in **Maxis's project** (`mxs-maxisapp-prd`) and needs their approval
> and IAM rights. The existing `ces-genesys-adapter-stg` (GECX bot) is **not touched**.

```
Genesys ─▶ ces-genesys-adapter-adk-stg (NEW, us-central1) ──Bearer <shared token>──▶ maxis-voice-agent-stg /ces/us
                 │ reads AUTH_TOKEN_SECRET_PATH                                        │ mounts the same secret as CES_BRIDGE_TOKEN
                 └──────────────── secret: voice-agent-ces-token-stg ◀─────────────────┘
Genesys ─▶ ces-genesys-adapter-stg (EXISTING, unchanged) ─▶ GECX voicebot-agent
```

## Names (proposal, change as Maxis prefers)

| Thing | Name |
|---|---|
| Shared token secret | `voice-agent-ces-token-stg` |
| Second adapter service | `ces-genesys-adapter-adk-stg` (us-central1, same as the existing one) |
| Second adapter's service account | `ces-adapter-adk-stg@mxs-maxisapp-prd.iam.gserviceaccount.com` |
| Its Genesys credentials | `genesys-adk-audioconnector-api-key-stg`, `genesys-adk-audioconnector-client-secret-stg` (new Audio Connector integration) |
| Our server | `maxis-voice-agent-stg` (**us-central1**, next to the adapter and Gemini), SA `maxis-voice-agent@…` |

## Step 1: shared token secret

The secret payload must be JSON with `access_token` and `expiry` (epoch **ms**). The adapter
caches the token until `expiry`, then re-reads `latest`. Use a far-future expiry and rotate by
adding a version and restarting both services.

```bash
P=mxs-maxisapp-prd
# token generated and piped straight in: never printed or kept in shell history
python3 -c 'import json,secrets;print(json.dumps({"access_token":secrets.token_urlsafe(48),"expiry":4102444800000}),end="")' \
  | gcloud secrets create voice-agent-ces-token-stg --project $P --replication-policy automatic --data-file=-

for SA in ces-adapter-adk-stg maxis-voice-agent; do
  gcloud secrets add-iam-policy-binding voice-agent-ces-token-stg --project $P \
    --member "serviceAccount:$SA@$P.iam.gserviceaccount.com" --role roles/secretmanager.secretAccessor
done
```

## Step 2: adapter code (one line)

In a checkout of the **same source** the existing adapter was built from (ask Maxis; the
Google repo `HEAD` behaves the same), in `src/ces_ws.py`:

```diff
+import os
@@
-_BASE_WS_URL = (
-    "wss://ces.googleapis.com/ws/google.cloud.ces.v1.SessionService/"
-    "BidiRunSession/locations/"
-)
+_BASE_WS_URL = os.getenv(
+    "CES_WS_URL",
+    "wss://ces.googleapis.com/ws/google.cloud.ces.v1.SessionService/BidiRunSession/locations/",
+)
```

Without `CES_WS_URL` the behaviour is unchanged, so the same code is safe for the GECX adapter too.

## Step 3: deploy our server

From `voice_agent_server/` with `.env` set for Maxis (`GOOGLE_CLOUD_PROJECT=mxs-maxisapp-prd`,
`CLOUD_RUN_REGION=us-central1`, `CLOUD_RUN_SERVICE=maxis-voice-agent-stg`, gateway, FAQ, ...):

```bash
CES_BRIDGE_TOKEN_SECRET=voice-agent-ces-token-stg ./deploy.sh
# prints wss://maxis-voice-agent-stg-<n>.us-central1.run.app/ces/<location>
curl -s https://maxis-voice-agent-stg-<n>.us-central1.run.app/healthz      # -> ok
```

Our server accepts the secret's JSON as is (it reads `access_token`). For a test service with
transcripts for the test client, use `DEPLOY_CES_DEBUG_EVENTS=true`. Keep it `false` once Genesys
traffic is on it.

## Step 4: deploy the second adapter

Same shape as the existing one (`ingress all`, no invoker IAM since Genesys can't send Google
tokens, `timeout 3600`, no CPU throttling), plus the two new env vars:

```bash
P=mxs-maxisapp-prd; R=us-central1
gcloud run deploy ces-genesys-adapter-adk-stg --project $P --region $R --source . \
  --service-account ces-adapter-adk-stg@$P.iam.gserviceaccount.com \
  --allow-unauthenticated --timeout 3600 --no-cpu-throttling --cpu-boost \
  --min-instances 1 --max-instances 50 --concurrency 80 --memory 512Mi \
  --set-env-vars "CES_WS_URL=wss://maxis-voice-agent-657431854992.us-central1.run.app/ces/,AUTH_TOKEN_SECRET_PATH=projects/$P/secrets/voice-agent-ces-token" \
  --set-secrets "GENESYS_API_KEY=genesys-adk-audioconnector-api-key-stg:latest,GENESYS_CLIENT_SECRET=genesys-adk-audioconnector-client-secret-stg:latest"
```

- `CES_WS_URL` for the deployed service (7 Nov, `maxis-voice-agent`, us-central1):
  **`wss://maxis-voice-agent-657431854992.us-central1.run.app/ces/`**. The other service URL
  `wss://maxis-voice-agent-uk6ub3gyvq-uc.a.run.app/ces/` reaches the same service.
- `AUTH_TOKEN_SECRET_PATH` must be the **full** resource name (`projects/.../secrets/...`); the
  adapter adds `/versions/latest`. The deployed service reads secret `voice-agent-ces-token`.
- `CES_WS_URL` must end with `/ces/`: the adapter appends the location from `_agent_id` (`us`),
  so it connects to `.../ces/us`.
- **Cloud Run IAM (org policy `run.managed.requireInvokerIam`)**: `maxis-voice-agent` only accepts
  calls with a Google **ID token** (audience = the service URL) in `X-Serverless-Authorization`. The
  adapter doesn't send one today, so it needs an extra patch, and its SA needs `roles/run.invoker`
  on `maxis-voice-agent`. The other option is an org policy exception for our service.
- **Decision (7 Nov):** the second adapter is a **replica of the existing one** and gets the **same
  org policy exception** (invoker IAM check off), so Genesys can reach it without Google tokens.
- **Decision (7 Nov), option A:** `maxis-voice-agent` **also gets the exception** (invoker IAM check
  off). The adapter then needs only `CES_WS_URL` + `AUTH_TOKEN_SECRET_PATH`; our server's shared
  token is the only check. Applying it is a Maxis change on our service (e.g.
  `gcloud run services update maxis-voice-agent --no-invoker-iam-check`), not done yet.
  **Later, option B (hardening):** turn the check back on, patch the adapter to also send a Google ID
  token in `X-Serverless-Authorization`, and grant its SA `roles/run.invoker` on `maxis-voice-agent`.
- The adapter's SA needs `secretAccessor` on all three secrets.
- Health: `GET /health/liveness` returns 200 once it can read the token secret. If it can't,
  Genesys connections are refused with WS 1013.

## Step 5: Genesys (Maxis Genesys owner)

1. New **Audio Connector** integration, base URI `wss://ces-genesys-adapter-adk-stg-<n>.us-central1.run.app/`,
   with the API key + client secret from step 4.
2. **Staging** inbound flow + test DID (not the GECX route): *Call Audio Connector* with input variables
   - `_agent_id = projects/mxs-maxisapp-prd/locations/us/apps/adk` (only `us` matters; the rest is a label)
   - `caller_id = Call.Ani` (same as the GECX flow)
3. After the connector: failure path → queue. Branch on output variables later (escalation is a separate task).

## Step 6: check

| Where | Expect |
|---|---|
| Our server logs | `CES bridge: call <conversationId> started (msisdn ...XXXX, variables ['adapterSessionId', 'caller_id'])` then `... ended (...)` |
| Adapter logs | `Connecting to CES ... url: wss://maxis-voice-agent-stg-.../ces/us`, `Sent session_start event`, `Received endSession`, `Preparing to send disconnect ... completed` |
| 401 in adapter logs (`CES Connection/Config Error`) | token mismatch: both services must read the same secret; restart after rotating |
| `Missing required parameter: _agent_id` | Architect input variables |

Before step 4 we can test the whole chain locally: the real adapter code (patched) against a
local server, with a fake Genesys client. Nothing in Maxis's project is needed for that test.
