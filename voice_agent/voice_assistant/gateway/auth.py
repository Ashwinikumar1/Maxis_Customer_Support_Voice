"""Google-signed OIDC ID tokens for the Maxis API gateway (Cloud Run, IAM-protected).

The gateway only accepts callers with roles/run.invoker, proven by an ID token in
`Authorization: Bearer ...`. Where the token comes from:

1. Cloud Run / GCE / a service-account key: google.oauth2.id_token.fetch_id_token
   with audience = the gateway URL (the runtime service account needs
   roles/run.invoker on the gateway service).
2. Local dev with user ADC (fetch_id_token can't mint user tokens):
   `gcloud auth print-identity-token`, optionally impersonating
   GATEWAY_IMPERSONATE_SA (needs roles/iam.serviceAccountTokenCreator on it).

Tokens are cached until ~5 minutes before expiry. Tokens are never logged.
"""

import base64
import json
import logging
import os
import subprocess
import threading
import time

import google.auth.exceptions
import google.auth.transport.requests
import google.oauth2.id_token

logger = logging.getLogger("voice_agent.gateway")

_REFRESH_MARGIN_S = 300
_lock = threading.Lock()
_cache: dict[str, tuple[str, float]] = {}  # audience -> (token, expiry epoch)
_use_gcloud = False  # sticky once the service-account path isn't available


def _expiry(token: str) -> float:
  """exp claim of a JWT (no signature check: we minted it, we only need the lifetime)."""
  try:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
  except Exception:
    return time.time() + 600  # unknown: refresh in 10 minutes


def _from_service_account(audience: str) -> str:
  return google.oauth2.id_token.fetch_id_token(
      google.auth.transport.requests.Request(), audience)


def _from_gcloud(audience: str) -> str:
  cmd = ["gcloud", "auth", "print-identity-token"]
  sa = os.getenv("GATEWAY_IMPERSONATE_SA", "").strip()
  if sa:  # service-account tokens can carry the gateway URL as audience
    cmd += [f"--impersonate-service-account={sa}", f"--audiences={audience}", "--include-email"]
  out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=True)
  token = out.stdout.strip()
  if not token:
    raise RuntimeError("gcloud returned no identity token")
  return token


def get_id_token(audience: str) -> str:
  """A valid ID token for `audience` (the gateway base URL). Raises on failure."""
  global _use_gcloud
  with _lock:
    cached = _cache.get(audience)
    if cached and cached[1] - _REFRESH_MARGIN_S > time.time():
      return cached[0]
    token = None
    if not _use_gcloud:
      try:
        token = _from_service_account(audience)
      except google.auth.exceptions.DefaultCredentialsError:
        _use_gcloud = True
        logger.info("Gateway auth: no service account; using local gcloud identity token")
    if token is None:
      token = _from_gcloud(audience)
    _cache[audience] = (token, _expiry(token))
    return token


def warm_up_token(audience: str) -> None:
  """Fetches (and caches) the gateway token ahead of the first tool call.

  Called in the background when a call starts, so the first roaming tool doesn't
  wait for the token (locally `gcloud print-identity-token` takes a few seconds).
  Never raises: if it fails, the tool call simply fetches the token itself.
  """
  if not audience:
    return
  try:
    get_id_token(audience)
  except Exception as e:  # noqa: BLE001
    logger.warning("Gateway token warm-up failed: %s", type(e).__name__)
