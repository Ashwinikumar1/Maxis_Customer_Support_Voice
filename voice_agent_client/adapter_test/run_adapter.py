"""Runs Google's ces-genesys-adapter (patched with CES_WS_URL) locally, unmodified otherwise.

Only two things are swapped, both outside the adapter's logic:
  - the Secret Manager read for AUTH_TOKEN_SECRET_PATH -> a local file with the SAME JSON
    ({"access_token", "expiry"}); the adapter's caching/expiry code still runs.
  - google.auth.default() (only used for the X-Goog-User-Project header) -> a fixed project.

env: ADAPTER_DIR, LOCAL_TOKEN_FILE, CES_WS_URL, PORT, GENESYS_API_KEY, GENESYS_CLIENT_SECRET
"""
import json
import os
import sys

sys.path.insert(0, os.environ["ADAPTER_DIR"])
os.environ.setdefault("AUTH_TOKEN_SECRET_PATH", "projects/local/secrets/voice-agent-ces-token-stg")

import google.auth  # noqa: E402

google.auth.default = lambda *a, **k: (None, "local-test-project")

from src import auth  # noqa: E402


async def _fetch_from_file(self):
  with open(os.environ["LOCAL_TOKEN_FILE"]) as f:
    data = json.load(f)
  if "access_token" not in data or "expiry" not in data:
    raise ValueError("Secret payload is missing 'access_token' or 'expiry'")
  self._token_info = {"access_token": data["access_token"], "expiry": data["expiry"]}


auth.Auth._fetch_token_from_secret_manager = _fetch_from_file

import uvicorn  # noqa: E402

from src.main import app  # noqa: E402

if __name__ == "__main__":
  uvicorn.run(app, host="127.0.0.1", port=int(os.environ["PORT"]), log_level="warning")
