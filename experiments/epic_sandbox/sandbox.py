"""One real Patient.read against Epic's public FHIR sandbox, two ways.
See docs/notes/ehr-landscape.md for the walkthrough and the SMART diagram.

    python -m experiments.epic_sandbox.sandbox keys                         # 1. key pair + cert to upload
    python -m experiments.epic_sandbox.sandbox backend --client-id <id>     # 2a. backend services, no user
    python -m experiments.epic_sandbox.sandbox standalone --client-id <id>  # 2b. SMART standalone launch

Both read the client ID from --client-id or EPIC_CLIENT_ID (a client ID is
an identifier, not a secret). The private key stays in .keys/, which git
ignores; neither command prints a token.

backend    -- SMART Backend Services: the app signs a JWT with its private key
              (RS384), trades it for an access token (client_credentials), and
              reads any patient the app is authorized for. No browser, no login.
              Register on fhir.epic.com as "Backend Systems" and upload
              .keys/publickey509.pem as the non-production key.
standalone -- SMART standalone launch for a patient-facing app: browser to
              Epic's authorize endpoint, the *patient* logs in to MyChart and
              approves, Epic redirects back here with a code, PKCE proves we're
              the one who asked, and the token response says which patient.
              Register as "Patients" audience with redirect URI
              http://localhost:8765/callback.

Epic: "changes may take up to 1 hour to sync with the Sandbox". A new
client ID that gets invalid_client for the first hour is normal.
"""

import argparse
import base64
import hashlib
import html
import json
import os
import secrets
import sys
import time
import uuid
import webbrowser
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

FHIR_BASE = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
# Fallbacks; the real values come from FHIR_BASE/.well-known/smart-configuration.
AUTHORIZE_URL = "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/authorize"
TOKEN_URL = "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token"
# Camila Lopez, one of Epic's sandbox test patients.
DEFAULT_PATIENT = "erXuFYUfucBZaryVksYEcMg3"
KEYS = Path(__file__).parent / ".keys"
PRIVATE_KEY = KEYS / "privatekey.pem"
CERT = KEYS / "publickey509.pem"
CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
REDIRECT_PORT = 8765


def discover(http: httpx.Client) -> dict:
    """SMART discovery: where to authorize, where to get tokens, what's supported."""
    response = http.get(f"{FHIR_BASE}/.well-known/smart-configuration")
    response.raise_for_status()
    return response.json()


# ── backend services ─────────────────────────────────────────────────────────
def make_keys(directory: Path = KEYS, common_name: str = "fde-voice-agent-sandbox") -> tuple[Path, Path]:
    """An RSA key pair, plus the public half as a self-signed X.509 cert:
    Epic wants "a base64 encoded X.509 certificate" uploaded, not a bare key.
    Same as `openssl genrsa` + `openssl req -new -x509`."""
    directory.mkdir(mode=0o700, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
            .sign(key, hashes.SHA256()))
    private, public = directory / PRIVATE_KEY.name, directory / CERT.name
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    private.chmod(0o600)
    public.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return private, public


def client_assertion(client_id: str, token_url: str, private_key_pem: bytes, *, now: float | None = None) -> str:
    """The signed JWT that *is* the backend app's credential. iss and sub are
    both the client ID, aud is the token endpoint, jti is single-use, and Epic
    rejects exp more than 5 minutes out."""
    now = int(now if now is not None else time.time())
    claims = {"iss": client_id, "sub": client_id, "aud": token_url, "jti": str(uuid.uuid4()),
              "iat": now, "nbf": now, "exp": now + 240}
    return jwt.encode(claims, private_key_pem, algorithm="RS384", headers={"typ": "JWT"})


def backend_token(http: httpx.Client, client_id: str, token_url: str) -> dict:
    response = http.post(token_url, data={
        "grant_type": "client_credentials",
        "client_assertion_type": CLIENT_ASSERTION_TYPE,
        "client_assertion": client_assertion(client_id, token_url, PRIVATE_KEY.read_bytes()),
    })
    _raise_for_oauth(response)
    return response.json()


# ── standalone launch ────────────────────────────────────────────────────────
def pkce_pair() -> tuple[str, str]:
    """PKCE: a random verifier kept here, its SHA-256 sent with the authorize
    request. Whoever redeems the code must show the verifier, so an
    intercepted code is useless. Public clients have no secret; this is it."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(authorize_endpoint: str, client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return authorize_endpoint + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        # launch/patient: "no EHR launched me, ask the user to pick a patient"
        # (for a MyChart login, that's themselves). Epic grants what the app
        # registration allows; the scope list is mostly declarative.
        "scope": "openid fhirUser launch/patient patient/Patient.read",
        "aud": FHIR_BASE,  # which FHIR server the token is for; Epic requires it
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def _wait_for_callback(port: int, open_url: str | None = None, timeout: float = 300) -> dict:
    """Claim the port, *then* send the user to `open_url`, and serve requests
    until /callback arrives; return its params. If Epic doesn't like the
    request (unknown or unsynced client ID, redirect URI mismatch) it shows an
    error page and never redirects, so give up after `timeout`."""
    received: dict = {}
    deadline = time.monotonic() + timeout

    class Handler(BaseHTTPRequestHandler):
        timeout = 5  # a browser's speculative preconnect sends nothing; don't block on it

        def do_GET(self):
            self._callback(urlparse(self.path).query)

        def do_POST(self):  # in case the redirect comes back as a form post
            length = int(self.headers.get("Content-Length") or 0)
            self._callback(self.rfile.read(length).decode())

        def _callback(self, query: str):
            if urlparse(self.path).path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            params = {k: v[0] for k, v in parse_qs(query).items()}
            if "code" in params or "error" in params:
                received.update(params)
                message = "Got the redirect. Back to the terminal."
            else:
                # Names only: values could be the code.
                print(f"callback without code or error: {self.command} {urlparse(self.path).path}, "
                      f"params {sorted(params) or 'none'}; still waiting", flush=True)
                message = "This request had no code. Still waiting for Epic's redirect."
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<p>{message}</p>".encode())

        def log_message(self, *args):  # the query string carries the code; keep it out of the log
            pass

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise SystemExit(f"port {port} is busy ({e.strerror}); another run of this script is probably "
                         f"still waiting. Stop it (Ctrl+C), or `lsof -iTCP:{port}` to find it.") from None
    with server:
        if open_url:
            print("Opening Epic's login page. Log in as a MyChart test patient and allow access.\n"
                  f"If no browser opens, visit:\n  {open_url}\n", flush=True)
            webbrowser.open(open_url)
        server.timeout = 1
        while not received:
            if time.monotonic() > deadline:
                raise SystemExit(f"no redirect back within {timeout:.0f}s. If Epic showed 'OAuth2 Error': "
                                 "the client ID may not be synced yet (up to 1 h), or the registered "
                                 f"redirect URI isn't exactly http://localhost:{port}/callback")
            server.handle_request()
    return received


def standalone_token(http: httpx.Client, client_id: str, smart: dict, port: int) -> dict:
    redirect_uri = f"http://localhost:{port}/callback"
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    url = authorize_url(smart.get("authorization_endpoint", AUTHORIZE_URL), client_id, redirect_uri, state, challenge)
    callback = _wait_for_callback(port, open_url=url)
    if callback.get("state") != state:
        raise SystemExit("state mismatch: this callback isn't the answer to our request")
    if "error" in callback:
        raise SystemExit(f"authorize failed: {callback['error']}: {callback.get('error_description', '')}")
    response = http.post(smart.get("token_endpoint", TOKEN_URL), data={
        "grant_type": "authorization_code", "code": callback["code"], "redirect_uri": redirect_uri,
        "client_id": client_id, "code_verifier": verifier,
    })
    _raise_for_oauth(response)
    return response.json()


# ── the call itself ──────────────────────────────────────────────────────────
def read_patient(http: httpx.Client, access_token: str, patient_id: str) -> tuple[str, int, dict]:
    url = f"{FHIR_BASE}/Patient/{patient_id}"
    response = http.get(url, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/fhir+json"})
    return url, response.status_code, response.json() if response.content else {}


def summarize(patient: dict) -> dict:
    name = next((n for n in patient.get("name", []) if n.get("use") == "official"), (patient.get("name") or [{}])[0])
    return {"id": patient.get("id"), "name": name.get("text") or " ".join([*name.get("given", []), name.get("family", "")]),
            "gender": patient.get("gender"), "birthDate": patient.get("birthDate"),
            "identifiers": len(patient.get("identifier", []))}


def _raise_for_oauth(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        body = response.json()
        detail = f"{body.get('error')}: {body.get('error_description', '')}"
    except ValueError:
        detail = response.text[:200]
    hint = " (a client ID under an hour old often isn't synced to the sandbox yet)" if "invalid_client" in detail else ""
    raise SystemExit(f"token request failed, HTTP {response.status_code}: {detail}{hint}")


def _result_page(mode: str, url: str, status: int, token: dict, patient: dict) -> Path:
    """A page to screenshot for the notes: the request, the status, the
    resource. No token in it, only what the token response said about scope."""
    granted = {k: token.get(k) for k in ("token_type", "scope", "expires_in", "patient") if k in token}
    page = Path(__file__).parent / "out" / "patient-read.html"
    page.parent.mkdir(exist_ok=True)
    page.write_text(f"""<!doctype html><meta charset="utf-8"><title>Epic sandbox Patient.read</title>
<style>body{{font:14px/1.45 -apple-system,system-ui,sans-serif;margin:32px;max-width:960px;color:#1d2433}}
code,pre{{font:12.5px ui-monospace,Menlo,monospace}}pre{{background:#f3f5f9;padding:14px;border-radius:6px;overflow:auto;max-height:520px}}
.ok{{color:#0a7a3d;font-weight:600}}.bad{{color:#b3261e;font-weight:600}}</style>
<h2>Epic on FHIR sandbox &middot; Patient.read</h2>
<p>{datetime.now().astimezone():%Y-%m-%d %H:%M %Z} &middot; flow: <b>{html.escape(mode)}</b></p>
<p><code>GET {html.escape(url)}</code> &rarr; <span class="{'ok' if status == 200 else 'bad'}">HTTP {status}</span></p>
<p>Token response (token itself omitted): <code>{html.escape(json.dumps(granted))}</code></p>
<pre>{html.escape(json.dumps(summarize(patient), indent=2))}</pre>
<details><summary>Full resource</summary><pre>{html.escape(json.dumps(patient, indent=2))}</pre></details>
""")
    return page


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m experiments.epic_sandbox.sandbox")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("keys", help="generate the key pair and the X.509 cert to upload")
    for mode in ("backend", "standalone"):
        p = sub.add_parser(mode)
        p.add_argument("--client-id", default=os.environ.get("EPIC_CLIENT_ID"))
        p.add_argument("--patient", default=DEFAULT_PATIENT, help="backend only; standalone uses the token's patient")
        p.add_argument("--port", type=int, default=REDIRECT_PORT)
        p.add_argument("--no-open", action="store_true", help="don't open the result page")
    args = parser.parse_args()

    if args.mode == "keys":
        if PRIVATE_KEY.exists():
            print(f"{PRIVATE_KEY} already exists; delete .keys/ to start over. Upload: {CERT}")
            return 0
        _, cert = make_keys()
        print(f"Upload this as the non-production public key on fhir.epic.com:\n  {cert}")
        return 0
    if not args.client_id:
        parser.error("--client-id or EPIC_CLIENT_ID is required")

    with httpx.Client(timeout=30) as http:
        smart = discover(http)
        if args.mode == "backend":
            if not PRIVATE_KEY.exists():
                parser.error("no key yet: run `keys` first and upload the cert")
            token = backend_token(http, args.client_id, smart.get("token_endpoint", TOKEN_URL))
            patient_id = args.patient
        else:
            token = standalone_token(http, args.client_id, smart, args.port)
            patient_id = token.get("patient") or args.patient
        url, status, patient = read_patient(http, token["access_token"], patient_id)

    print(f"GET {url} -> HTTP {status}")
    print(json.dumps(summarize(patient) if status == 200 else patient, indent=2))
    page = _result_page(args.mode, url, status, token, patient)
    print(f"\nResult page for the screenshot: {page}")
    if not args.no_open:
        webbrowser.open(page.as_uri())
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
