# Genesys end-to-end test: ADK voice agent (staging)

_9 Nov 2026. Project `mxs-maxisapp-prd`. Shareable: contains no secret values._

**Goal:** one staging phone call: Genesys → `ces-genesys-adapter-adk-stg` → `maxis-voice-agent`.
The existing GECX adapter (`ces-genesys-adapter-stg`) and its route stay unchanged.

```
Caller ─▶ Genesys (staging flow + test DID)
            └─ Call Audio Connector ─▶ ces-genesys-adapter-adk-stg (Cloud Run, us-central1)   NEW
                                          └─▶ maxis-voice-agent (Cloud Run, us-central1)       NEW
                                                 └─▶ Gemini Live + Maxis agent gateway
```

## 0. Prerequisite (Maxis GCP admin, before Genesys can connect)

| # | Action | Why |
|---|---|---|
| 0.1 | Bind tag `mxs-maxisapp-prd/Agentassist-PRD/Agentassist-PRD-Tags` (`tagValues/281475455074817`) to `ces-genesys-adapter-adk-stg`, the same tag `ces-genesys-adapter-stg` has (needs `roles/resourcemanager.tagUser` on that tag):<br>`gcloud resource-manager tags bindings create --tag-value=tagValues/281475455074817 --parent=//run.googleapis.com/projects/mxs-maxisapp-prd/locations/us-central1/services/ces-genesys-adapter-adk-stg --location=us-central1` | The project's `run.managed.requireInvokerIam` policy is **not enforced** on resources with this tag (rule `genesys-audio-connector`). Without it, the IAM check can't be turned off |
| 0.2 | Then: `gcloud run services update ces-genesys-adapter-adk-stg --region us-central1 --project mxs-maxisapp-prd --no-invoker-iam-check` | Genesys can't send Google identity tokens; without this Cloud Run returns **403**. Genesys is still authenticated by the adapter (API key + HMAC signature) |

`maxis-voice-agent` does **not** need an exception: the adapter calls it with a Google ID token
(its service account has `roles/run.invoker`), plus a shared token from Secret Manager.

## 1. Audio Connector integration (Genesys admin)

Create a **new** Audio Connector integration (don't edit the GECX one):

| Field | Value |
|---|---|
| Base Connection URI | `wss://ces-genesys-adapter-adk-stg-657431854992.us-central1.run.app/` |
| API Key | **Same** as the existing staging GECX Audio Connector integration |
| Client Secret | **Same** as the existing staging GECX Audio Connector integration |

The new adapter reads the same Secret Manager secrets as the GECX adapter
(`genesys-audioconnector-api-key-stg`, `genesys-audioconnector-client-secret-stg`), so the
existing credential pair works. If you prefer a separate pair, generate a new one and tell us the
two new secret names; we'll redeploy (values go into Secret Manager only, never by email/chat).

Connector ID: any value works (the adapter accepts any path after the base URI).

## 2. Architect: staging inbound flow + test DID

Use a **staging** flow and test DID, not the production/GECX route.

**Call Audio Connector** → the new integration, with input variables:

| Input variable | Value | Notes |
|---|---|---|
| `_agent_id` | `projects/mxs-maxisapp-prd/locations/us/apps/adk` | **Required.** Only `us` is used (routing); the rest is a label. The GECX flow's existing value (`.../locations/us/apps/7d88a744-...`) also works; `apps/adk` just makes logs easier to tell apart |
| `caller_id` | `Call.Ani` | Same as the GECX flow. The agent uses it as the caller's Maxis number for account lookups |

Paths after the connector:

| Path | Action |
|---|---|
| Failure | Transfer to the normal queue |
| Success | First test: just disconnect (don't branch on output variables yet). Branching is phase 2, after voice works |

### Output variables the agent returns (all strings)

| Variable | Values |
|---|---|
| `session_escalated` | `"true"` / `"false"` |
| `outcome` | `handoff` / `end` |
| `handoff_reason`, `end_reason` | short reason |
| `summary`, `customer_request` | what the caller wanted |
| `use_case`, `destination`, `pass_name` | roaming details when relevant |

## 3. Test calls (both teams on a bridge)

Call the test DID from a **Maxis number on the staging/test list**, then:

1. Greeting plays within ~3 s.
2. FAQ question, e.g. "How much is the roaming day pass for Japan?"
3. Account question, e.g. "Is roaming on for my line?"
4. Say "I want to talk to an agent" → call ends; we check in the logs that `session_escalated = "true"` reached Genesys (no queue routing yet).
5. Hang up mid-sentence → call ends cleanly (no stuck leg).

We watch Cloud Run logs live (`ces-genesys-adapter-adk-stg`, `maxis-voice-agent`) during the calls.

## 4. Known limits in this test build

- Gateway **write** actions (buy pass, switch roaming, wrong-pass help) are **switched off** in code;
  the agent can only read account and roaming info.
- The agent calls the **PROD** gateway (read only). Tell us if staging should use the STG gateway.
- Escalation output names are our proposal; see question 1.

## 5. Questions for the Genesys team

1. Which output variables does Architect branch on after the GECX bot today (e.g. `intent`,
   `summary`, `language`)? We'll match the names.
2. Staging DID and the test caller numbers you'll use (we need numbers the gateway recognises).
3. Should the failure path go to the same queue as the GECX flow?
