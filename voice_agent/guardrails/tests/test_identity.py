"""IAP header checks (guardrails/identity.py), with locally generated keys: no network needed.

Each test builds a token exactly as IAP would (ES256, kid header, aud/iss/iat/exp/sub claims)
and changes one thing to prove that one check catches it.
"""
import asyncio
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from google.auth import crypt, jwt

from guardrails import events
from guardrails.identity import (IAP_HEADER, IAP_ISSUER, Caller, IapVerifier, IdentityError,
                                 identify, verifier_from_env)

AUD = "/projects/123/global/backendServices/456"


def _keypair(kid: str):
  key = ec.generate_private_key(ec.SECP256R1())
  private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())
  public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                             serialization.PublicFormat.SubjectPublicKeyInfo)
  return crypt.ES256Signer.from_string(private_pem, key_id=kid), public_pem.decode()


SIGNER, PUBLIC = _keypair("k1")
OTHER_SIGNER, _ = _keypair("k1")  # same key ID, different key: a forgery


def _token(signer=SIGNER, **overrides) -> str:
  now = int(time.time())
  claims = {"aud": AUD, "iss": IAP_ISSUER, "iat": now, "exp": now + 600,
            "sub": "accounts.google.com:1234567890", "email": "tester@example.com"}
  claims.update(overrides)
  claims = {k: v for k, v in claims.items() if v is not None}
  return jwt.encode(signer, claims).decode()


def _verifier(fetch=lambda: {"k1": PUBLIC}) -> IapVerifier:
  return IapVerifier(AUD, fetch=fetch)


def _verify(token, verifier=None):
  return asyncio.run((verifier or _verifier()).verify({IAP_HEADER: token}))


def test_valid_header_gives_a_hashed_user_id(monkeypatch):
  monkeypatch.setenv("SECURITY_EVENT_HMAC_KEY", "k")
  caller = _verify(_token())
  assert caller.verified and caller.user_id.startswith("iap_")
  # Neither the email nor the raw account ID may appear in the ID ADK stores.
  assert "1234567890" not in caller.user_id and "tester" not in caller.user_id
  assert _verify(_token()).user_id == caller.user_id  # stable across calls


@pytest.mark.parametrize("token,reason", [
    (None, "iap_header_missing"),
    ("not-a-jwt", "iap_invalid"),
], ids=["missing", "garbage"])
def test_missing_or_garbage_header_refused(token, reason):
  with pytest.raises(IdentityError) as exc:
    asyncio.run(_verifier().verify({IAP_HEADER: token} if token else {}))
  assert exc.value.reason.startswith(reason)


@pytest.mark.parametrize("overrides", [
    {"aud": "/projects/123/global/backendServices/999"},  # token for another backend
    {"exp": int(time.time()) - 120},                      # expired
    {"iat": int(time.time()) + 3600, "exp": int(time.time()) + 7200},  # issued in the future
], ids=["wrong_audience", "expired", "future"])
def test_claim_checks(overrides):
  with pytest.raises(IdentityError):
    _verify(_token(**overrides))


def test_forged_signature_refused():
  with pytest.raises(IdentityError):
    _verify(_token(signer=OTHER_SIGNER))


def test_wrong_issuer_refused():
  with pytest.raises(IdentityError) as exc:
    _verify(_token(iss="https://accounts.google.com"))
  assert exc.value.reason == "iap_wrong_issuer"


def test_missing_subject_refused():
  with pytest.raises(IdentityError) as exc:
    _verify(_token(sub=None))
  assert exc.value.reason == "iap_no_subject"


def test_keys_cached_and_fail_closed():
  calls = []

  def fetch():
    calls.append(1)
    if len(calls) > 1:
      raise OSError("network down")
    return {"k1": PUBLIC}

  v = _verifier(fetch)
  _verify(_token(), v)
  _verify(_token(), v)
  assert len(calls) == 1  # second call used the cache

  with pytest.raises(IdentityError) as exc:
    _verify(_token(), _verifier(lambda: (_ for _ in ()).throw(OSError("down"))))
  assert exc.value.reason == "iap_keys_unavailable"  # no keys at all: refuse, never allow


def test_identify_off_gives_anonymous_caller():
  caller = asyncio.run(identify(IapVerifier(""), {}, anonymous_id="abc"))
  assert caller == Caller(user_id="user_abc", verified=False)


def test_identify_on_logs_a_security_event(capsys):
  with pytest.raises(IdentityError):
    asyncio.run(identify(_verifier(), {}, anonymous_id="abc"))
  out = capsys.readouterr().out
  assert '"security_event": "identity_rejected"' in out and "iap_header_missing" in out


def test_require_iap_without_audience_stops_start_up(monkeypatch):
  monkeypatch.setenv("REQUIRE_IAP", "true")
  monkeypatch.delenv("IAP_AUDIENCE", raising=False)
  with pytest.raises(RuntimeError):
    verifier_from_env()
  monkeypatch.setenv("IAP_AUDIENCE", AUD)
  assert verifier_from_env().enabled


def test_hash_id_matches_event_hash(monkeypatch):
  monkeypatch.setenv("SECURITY_EVENT_HMAC_KEY", "k")
  assert events.build_event("x", session="session_1")["session"] == events.hash_id("session_1")
