# Archive

Code kept for reference only. Not imported by the app and not copied into the
Docker image (the Dockerfile copies only `streaming_core/`, `channels/`,
`voice_assistant/`, `static/` and `server.py`).

| File | What it was | Replaced by |
|---|---|---|
| `faq_search_tool.py` | `search_knowledge_base` function tool: Discovery Engine search on a Vertex AI Search *app* (`FAQ_SEARCH_ENGINE_ID`), top 3 snippets | ADK built-in `VertexAiSearchTool(data_store_id=FAQ_DATASTORE_ID)` on `maxis_agent`, grounded natively inside the Gemini Live turn |
| `bench_faq_search.py` | Latency benchmark for `faq_search_tool.py` | Search evidence is logged per turn (`voice_agent.live: Vertex AI Search grounding: queries=[...]`) |

Why it was replaced: the Maxis CX Agent Studio data stores are website data
stores attached to Standard-edition chat apps, so the raw Discovery Engine
search API rejects them ("Cannot use enterprise edition features"). Gemini
grounding on the data store works without an Enterprise search app.

To run the benchmark again, move both files back
(`voice_assistant/faq_search_tool.py`, `scripts/bench_faq_search.py`).

## Mocks (archived 1 Nov 2026)

| File | What it was |
|---|---|
| `mocks/gateway_mock.py` | In-process gateway stand-in (`voice_assistant/gateway/mock.py`), used when `GATEWAY_URL` was empty |
| `mocks/roaming_mock_backend.py` | Dummy roaming data and rules (`flows/roaming/mock_backend.py`) |
| `mocks/shared_mock_backend.py` | Dummy WhatsApp / handoff answers (`shared/mock_backend.py`) |

Replaced by real calls to the Maxis agent gateway (staging/prod) in
`voice_assistant/gateway/client.py`. The gateway has no handoff API, so
`escalateToAgent` now records the handoff outcome locally for the channel.
