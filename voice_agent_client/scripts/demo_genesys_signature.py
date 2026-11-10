"""Demo: how a Genesys-signed request is checked by the adapter.

Signs a fake WebSocket request exactly like Genesys Audio Connector does
(API key + HMAC-SHA256 per RFC 9421) and passes it to the REAL check in
genesys_adapter/src/auth.py (Auth.verify_request). Then tampers with it.

Dummy keys only; no network, no Google Cloud, no real secrets.
Run:  python3 voice_agent_client/scripts/demo_genesys_signature.py
"""
import base64
import hashlib
import hmac
import os
import sys
import time
import types
from pathlib import Path

# --- Dummy credentials (what the Genesys admin would type into the integration) ---
API_KEY = "demo-api-key-123"
CLIENT_SECRET_B64 = base64.b64encode(b"demo-client-secret-only-genesys-and-maxis-know").decode()

# Load the adapter's real auth.py. Its cloud imports (google.auth, Secret Manager,
# dotenv) are not used by the signature check, so they are stubbed here; the
# stub for dotenv also guarantees the real genesys_adapter/.env is NOT loaded.
for name in ("google", "google.auth", "google.auth.transport", "google.auth.transport.requests",
             "google.cloud", "google.cloud.secretmanager", "dotenv"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None
sys.modules["google.cloud"].secretmanager = sys.modules["google.cloud.secretmanager"]
sys.modules["google.auth"].transport = sys.modules["google.auth.transport"]
sys.modules["google.auth.transport"].requests = sys.modules["google.auth.transport.requests"]
os.environ["GENESYS_API_KEY"] = API_KEY
os.environ["GENESYS_CLIENT_SECRET"] = CLIENT_SECRET_B64
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "genesys_adapter"))
from src.auth import Auth  # noqa: E402  (the real adapter code)

HOST = "ces-genesys-adapter-adk-stg-657431854992.asia-southeast1.run.app"
COMPONENTS = ["@request-target", "@authority", "audiohook-organization-id",
              "audiohook-session-id", "audiohook-correlation-id", "x-api-key"]


class Headers(dict):
    """Case-insensitive headers, like Starlette's WebSocket.headers in the real adapter."""
    def get(self, key, default=None):
        return super().get(key.lower(), default)


class Request:
    """Minimal stand-in for the incoming WebSocket upgrade request."""
    def __init__(self, path, headers):
        self.path = path
        self.headers = Headers({k.lower(): v for k, v in headers.items()})


def genesys_sign(path, headers, secret_b64):
    """What GENESYS does before sending: build the RFC 9421 text, HMAC it."""
    now = int(time.time())
    comps = " ".join(f'"{c}"' for c in COMPONENTS)
    params = (f'keyid="{headers["x-api-key"]}";nonce="{base64.b64encode(os.urandom(12)).decode()}";'
              f'alg="hmac-sha256";created={now};expires={now + 60}')
    lines = []
    for c in COMPONENTS:
        value = path if c == "@request-target" else headers["host"] if c == "@authority" else headers[c]
        lines.append(f'"{c}": {value}')
    lines.append(f'"@signature-params": ({comps});{params}')
    text = "\n".join(lines)
    sig = base64.b64encode(hmac.new(base64.b64decode(secret_b64), text.encode(), hashlib.sha256).digest()).decode()
    signed = dict(headers)
    signed["signature-input"] = f"sig1=({comps});{params}"
    signed["signature"] = f"sig1=:{sig}:"
    return signed, text, sig


def base_headers():
    return {"host": HOST, "x-api-key": API_KEY,
            "audiohook-organization-id": "11111111-aaaa-bbbb-cccc-000000000001",
            "audiohook-session-id": "22222222-aaaa-bbbb-cccc-000000000002",
            "audiohook-correlation-id": "33333333-aaaa-bbbb-cccc-000000000003"}


def check(title, request):
    ok = Auth().verify_request(request)
    print(f"{'ACCEPTED' if ok else 'REJECTED (HTTP 403)':<20} {title}")


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)  # hide the adapter's own log lines

    signed, text, sig = genesys_sign("/", base_headers(), CLIENT_SECRET_B64)
    print("=== 1. What Genesys signs (the RFC 9421 text) ===")
    print(text)
    print("\n=== 2. HMAC-SHA256(client secret, text) -> sent in the Signature header ===")
    print(sig)
    print("\n=== 3. The adapter's real check (src/auth.py) on different requests ===")

    check("Genuine Genesys request", Request("/", signed))

    h = dict(signed); h["audiohook-session-id"] = "99999999-hacked"
    check("Attacker changed the session id after signing", Request("/", h))

    h = dict(signed); h["x-api-key"] = "guessed-key"
    check("Wrong API key", Request("/", h))

    h = {k: v for k, v in signed.items() if k not in ("signature", "signature-input")}
    check("Correct API key but NO signature (stolen key only)", Request("/", h))

    forged, _, _ = genesys_sign("/", base_headers(), base64.b64encode(b"attacker-guess").decode())
    check("Attacker signs with a guessed secret", Request("/", forged))

    check("Same genuine request sent again (replay)", Request("/", signed))
    print("\nNote: the replay is ACCEPTED today: created/expires/nonce are not checked yet (planned fix).")
