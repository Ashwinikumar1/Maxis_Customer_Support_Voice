# Maxis Voice Agent

Real-time bidi-streaming voice agent built with Google ADK and Gemini Live on Vertex AI.
It speaks English and Malay, uses Google Search, gets directions via the Google Maps Routes API,
and saves each session's transcript to GCS.

```
.
└── voice_agent/        ← active project (see voice_agent/README.md to run it)
    ├── server.py           WebSocket server + browser UI
    ├── streaming_core/     reusable live-session loop + GCS transcript logger
    ├── voice_assistant/    ADK agents and tools
    └── static/             browser UI
```

Quick start:
```bash
cd voice_agent
uv sync
uv run python server.py   # http://127.0.0.1:8080
```
