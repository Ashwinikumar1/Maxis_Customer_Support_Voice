"""Who is on the other end of /ws: the signed IAP header (baseline CR-02, AG-03).

Why this exists
---------------
Until now every connection got a random `user_id`, so the server had no idea who was calling.
In browser testing the load balancer puts Identity-Aware Proxy (IAP) in front of the service
(security/terraform/modules/network/edge.tf, `enable_iap = true`). IAP signs in the tester with
Google and then adds a header to every request it forwards:

    x-goog-iap-jwt-assertion: <JSON Web Token signed by Google with ES256>

Checking that header in the app as well as at the load balancer is "defence in depth": if someone
ever reaches Cloud Run directly (ingress opened by mistake, a new route, a misconfigured
backend), the header is missing or forged and the call is refused here too.

What is checked, and why each check matters
-------------------------------------------
1. Signature: the token must be signed by one of Google's current IAP keys, downloaded from
   https://www.gstatic.com/iap/verify/public_key. Without this anyone could write the header.
2. Audience (`aud`): must equal IAP_AUDIENCE, which is
   "/projects/<project number>/global/backendServices/<backend id>" for *our* backend
   (Terraform output `app_env.IAP_AUDIENCE`). Stops a token minted for another app being replayed.
3. Issuer (`iss`): must be "https://cloud.google.com/iap".
4. Time: `exp` in the future and `iat` not in the future, allowing 30 seconds of clock skew.
5. Subject (`sub`): must be present; it is Google's stable, opaque ID for the person.

What the app does with the result
---------------------------------
`user_id` becomes "iap_" + a hash of `sub`. The ADK session store and the logs therefore never
hold the tester's email or raw account ID, but every call by the same person shares one user_id,
which is what an investigation needs. The email is read only to log its domain on refusal.

When IAP_AUDIENCE is empty (local development, or the public demo before the edge exists) the
check is off and callers get a random anonymous ID, exactly as before. Set REQUIRE_IAP=true in
any shared environment: the server then refuses to start without IAP_AUDIENCE, so the check can't
be switched off by forgetting a variable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass

from .events import hash_id, security_event

log = logging.getLogger(__name__)

IAP_HEADER = "x-goog-iap-jwt-assertion"
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_KEYS_URL = "https://www.gstatic.com/iap/verify/public_key"
# Google rotates IAP keys; refetching hourly picks up new keys well before old ones are retired.
KEYS_TTL_SECONDS = 3600
CLOCK_SKEW_SECONDS = 30


@dataclass(frozen=True)
class Caller:
  """The identity a connection is allowed to act as.

  user_id   what ADK sessions are keyed by; never an email or a raw account ID
  verified  True only when it came from a valid IAP header
  """
  user_id: str
  verified: bool


class IdentityError(Exception):
  """The request must be refused. `reason` is a short code for logs and security events."""

  def __init__(self, reason: str):
    super().__init__(reason)
    self.reason = reason


class IapVerifier:
  """Verifies IAP headers. One instance is shared by every connection (keys are cached)."""

  def __init__(self, audience: str, keys_url: str = IAP_KEYS_URL, fetch=None):
    self.audience = audience
    self.keys_url = keys_url
    # `fetch` is injectable so tests can supply keys without the network.
    self._fetch = fetch or self._download_keys
    self._keys: dict[str, str] = {}
    self._keys_fetched_at = 0.0
    # One lock so a burst of new connections triggers a single download, not one each.
    self._lock = asyncio.Lock()

  @property
  def enabled(self) -> bool:
    return bool(self.audience)

  def _download_keys(self) -> dict[str, str]:
    # Plain HTTPS GET of a public JSON document: {"<key id>": "<PEM public key>", ...}.
    # urllib verifies the server certificate by default; the certifi bundle is set in server.py.
    with urllib.request.urlopen(self.keys_url, timeout=5) as resp:  # noqa: S310 (fixed https URL)
      return json.loads(resp.read())

  async def _current_keys(self) -> dict[str, str]:
    async with self._lock:
      if not self._keys or time.monotonic() - self._keys_fetched_at > KEYS_TTL_SECONDS:
        try:
          # The download is blocking I/O, so run it in a worker thread and keep the event loop
          # (and every other live call on this instance) responsive.
          self._keys = await asyncio.to_thread(self._fetch)
          self._keys_fetched_at = time.monotonic()
        except Exception:  # network failure: keep using cached keys if we have any
          log.exception("Could not refresh IAP keys")
          if not self._keys:
            # Fail closed: with no keys we cannot tell a real header from a forged one.
            raise IdentityError("iap_keys_unavailable")
      return self._keys

  async def verify(self, headers) -> Caller:
    """Returns the Caller for these request headers, or raises IdentityError."""
    token = headers.get(IAP_HEADER)
    if not token:
      raise IdentityError("iap_header_missing")
    keys = await self._current_keys()
    # google.auth.jwt.decode checks the signature against the key named in the token's `kid`,
    # the audience, and exp/iat (with the allowed skew). It raises ValueError on any failure.
    from google.auth import jwt

    try:
      claims = jwt.decode(token, certs=keys, audience=self.audience,
                          clock_skew_in_seconds=CLOCK_SKEW_SECONDS)
    except Exception as exc:  # google-auth raises ValueError subclasses; treat anything as invalid
      # Keep only the first few words of the library's message: enough to tell "expired" from
      # "wrong audience" in the logs, never the token itself.
      raise IdentityError("iap_invalid:" + "_".join(str(exc).lower().split()[:3])) from exc
    if claims.get("iss") != IAP_ISSUER:
      raise IdentityError("iap_wrong_issuer")
    sub = claims.get("sub")
    if not sub:
      raise IdentityError("iap_no_subject")
    return Caller(user_id="iap_" + hash_id(sub), verified=True)


def verifier_from_env() -> IapVerifier:
  """Builds the verifier from IAP_AUDIENCE, enforcing REQUIRE_IAP at start-up."""
  audience = os.getenv("IAP_AUDIENCE", "").strip()
  if os.getenv("REQUIRE_IAP", "false").lower() == "true" and not audience:
    # Refusing to start is deliberate: a shared environment that silently ran without the
    # identity check would look fine in testing and leave the service open.
    raise RuntimeError("REQUIRE_IAP=true but IAP_AUDIENCE is empty")
  if audience:
    log.info("IAP identity check on for /ws")
  else:
    log.warning("IAP_AUDIENCE not set: /ws callers are anonymous (local development only)")
  return IapVerifier(audience)


async def identify(verifier: IapVerifier, headers, anonymous_id: str) -> Caller:
  """The one call server.py makes, for every /ws connection.

  With the check off, returns an anonymous caller keyed by the connection's random ID.
  With it on, returns the verified caller, or logs a security event and raises IdentityError.
  """
  if not verifier.enabled:
    return Caller(user_id=f"user_{anonymous_id}", verified=False)
  try:
    return await verifier.verify(headers)
  except IdentityError as exc:
    # The detection "Forged or replayed connection" in logging-scc can match on this event.
    security_event("identity_rejected", reason=exc.reason, source="iap", severity="WARNING")
    raise
