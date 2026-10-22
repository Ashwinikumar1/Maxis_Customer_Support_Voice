"""Observability add-on for the Maxis voice agent.

Adds structured, trace-correlated logging, error status on spans, session
metrics, FAQ-search tracing and graceful SIGTERM flushing WITHOUT editing any
existing file. It is installed around the unchanged `server.py` by the
launcher in `observability/__main__.py`:

    uv run python -m observability          # instead of: uv run python server.py

Everything is opt-out through environment variables (see README.md).
"""

from .bootstrap import install

__all__ = ["install"]
