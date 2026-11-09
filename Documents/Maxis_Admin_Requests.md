# Maxis GCP admin: requests for the ADK voice agent

_9 Nov 2026. Project `mxs-maxisapp-prd`. Shareable: no secret values. Our account
(`<deployer>` below) can't create service accounts or bind tags, so these need a Maxis admin._

## A. Dedicated service accounts (replaces the shared default compute SA)

```bash
P=mxs-maxisapp-prd
VA=adk-voice-agent@$P.iam.gserviceaccount.com        # for Cloud Run maxis-voice-agent
AD=adk-genesys-adapter@$P.iam.gserviceaccount.com    # for Cloud Run ces-genesys-adapter-adk-stg
DEPLOYER=user:<deployer>                              # our account, to deploy services with these SAs

gcloud iam service-accounts create adk-voice-agent     --project $P --display-name "ADK voice agent (maxis-voice-agent)"
gcloud iam service-accounts create adk-genesys-adapter --project $P --display-name "ADK Genesys adapter (ces-genesys-adapter-adk-stg)"

# Voice agent: Gemini Live, FAQ data store, shared token, PROD gateway (read calls only)
gcloud projects add-iam-policy-binding $P --member serviceAccount:$VA --role roles/aiplatform.user --condition None
gcloud projects add-iam-policy-binding $P --member serviceAccount:$VA --role roles/discoveryengine.viewer --condition None
gcloud secrets add-iam-policy-binding voice-agent-ces-token --project $P --member serviceAccount:$VA --role roles/secretmanager.secretAccessor
gcloud run services add-iam-policy-binding voicebot-cx-studio-agent-gateway --project $P --region asia-southeast1 \
  --member serviceAccount:$VA --role roles/run.invoker

# Adapter: shared token + Genesys credentials, may call the voice agent
for S in voice-agent-ces-token genesys-audioconnector-api-key-stg genesys-audioconnector-client-secret-stg; do
  gcloud secrets add-iam-policy-binding $S --project $P --member serviceAccount:$AD --role roles/secretmanager.secretAccessor
done
gcloud run services add-iam-policy-binding maxis-voice-agent --project $P --region us-central1 \
  --member serviceAccount:$AD --role roles/run.invoker

# Let our account deploy Cloud Run services that run as these SAs
for SA in $VA $AD; do
  gcloud iam service-accounts add-iam-policy-binding $SA --project $P --member $DEPLOYER --role roles/iam.serviceAccountUser
done
```

After this, **we** switch both services to the new SAs, test a call, and then remove the temporary
grants on the shared default compute SA (`657431854992-compute@`): `roles/run.invoker` on
`voicebot-cx-studio-agent-gateway`, `voicebot-cx-studio-agent-gateway-stg` and `maxis-voice-agent`.
Nothing else on that SA changes (the existing GECX adapter uses it).

## B. Let Genesys reach the new adapter (needed for the Genesys test call)

The project policy `run.managed.requireInvokerIam` is not enforced on resources tagged
`mxs-maxisapp-prd/Agentassist-PRD/Agentassist-PRD-Tags` (the existing `ces-genesys-adapter-stg` has it).

```bash
gcloud resource-manager tags bindings create --tag-value=tagValues/281475455074817 \
  --parent=//run.googleapis.com/projects/mxs-maxisapp-prd/locations/us-central1/services/ces-genesys-adapter-adk-stg \
  --location=us-central1
gcloud run services update ces-genesys-adapter-adk-stg --region us-central1 --project mxs-maxisapp-prd --no-invoker-iam-check
```

Genesys is still authenticated by the adapter (API key + HMAC signature). `maxis-voice-agent` keeps
its IAM check on.
