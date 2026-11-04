# Maxis Voice Agent: Genesys Integration Summary

| | |
|---|---|
| Audience | Maxis Genesys Cloud team |
| Version | 0.1 (draft) |
| Date | 4 November 2026 |

## 1. Overview

The Maxis Voice Agent (Gemini Live on Google Cloud Run) connects to Genesys Cloud through the standard **Genesys Audio Connector**, the same method used today for the CX Studio voicebot.

1. The caller reaches a Genesys Architect flow, which runs **Call Audio Connector**.
2. Genesys streams the call over secure WebSocket (`wss://`) to the voice agent.
3. The voice agent ends the call or hands it off. On handoff, Genesys transfers the call to a queue.
4. If the voice agent is unreachable, the flow transfers the call to a default queue.

## 2. What we provide

| Item | Value |
|---|---|
| Connection URI | `wss://<voice-agent-host>/genesys` (after staging deployment) |
| Audio format | PCMU, 8 kHz, mono |
| Output variables | `outcome`, `handoff_queue`, `summary`, `language` |

## 3. What we need from Genesys

| # | Item |
|---|---|
| 1 | Named owner for Genesys changes |
| 2 | **API key and client secret** for a new Audio Connector integration (shared securely) |
| 3 | Genesys Cloud region and outbound IP ranges |
| 4 | Staging test flow and test number (DID) |
| 5 | Handoff queue names (roaming English and Malay) |
| 6 | Input variables available from the IVR (e.g. `language`) |

## 4. Genesys configuration

| Setting | Value |
|---|---|
| Integration | Audio Connector: `Maxis Voice Agent (STG)` |
| Base Connection URI | `wss://<voice-agent-host>/genesys` |
| Credentials | API key and client secret |
| Architect action | Call Audio Connector, Connector ID `roaming` |
| Success path | If `outcome` is handoff, transfer to `handoff_queue`, otherwise disconnect |
| Failure path | Transfer to a default queue |

## 5. Next steps

| Step | Activity | Owner |
|---|---|---|
| 1 | Share API key, client secret, region and queue names | Maxis Genesys |
| 2 | Deploy voice agent to Cloud Run (staging) and share URI | Google |
| 3 | Create integration, test flow and test number | Maxis Genesys |
| 4 | End-to-end test call, including handoff | Joint |
