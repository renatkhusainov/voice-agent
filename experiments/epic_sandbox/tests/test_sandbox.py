"""experiments/epic_sandbox/sandbox.py: the two pieces of SMART auth that are
easy to get subtly wrong, checked offline. The real call is the CLI."""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography import x509

from experiments.epic_sandbox.sandbox import (
    FHIR_BASE,
    TOKEN_URL,
    authorize_url,
    client_assertion,
    make_keys,
    pkce_pair,
    summarize,
)


def test_keys_are_a_private_key_and_an_x509_cert(tmp_path):
    private, public = make_keys(tmp_path)

    cert = x509.load_pem_x509_certificate(public.read_bytes())
    assert cert.public_key().key_size == 2048
    assert private.stat().st_mode & 0o077 == 0  # owner-only


def test_client_assertion_has_the_claims_epic_checks(tmp_path):
    private, public = make_keys(tmp_path)
    key = x509.load_pem_x509_certificate(public.read_bytes()).public_key()

    token = client_assertion("my-client-id", TOKEN_URL, private.read_bytes(), now=1_800_000_000)

    assert jwt.get_unverified_header(token)["alg"] == "RS384"
    claims = jwt.decode(token, key, algorithms=["RS384"], audience=TOKEN_URL,
                        options={"verify_exp": False, "verify_nbf": False, "verify_iat": False})
    assert claims["iss"] == claims["sub"] == "my-client-id"
    assert claims["exp"] - claims["iat"] <= 300  # Epic: at most 5 minutes
    assert claims["jti"]


def test_each_assertion_has_a_fresh_jti(tmp_path):
    private, _ = make_keys(tmp_path)

    a = jwt.decode(client_assertion("c", TOKEN_URL, private.read_bytes()), options={"verify_signature": False})
    b = jwt.decode(client_assertion("c", TOKEN_URL, private.read_bytes()), options={"verify_signature": False})

    assert a["jti"] != b["jti"]


def test_pkce_challenge_is_the_s256_of_the_verifier():
    verifier, challenge = pkce_pair()

    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected and 43 <= len(verifier) <= 128


def test_authorize_url_carries_aud_state_and_the_challenge():
    url = authorize_url("https://epic.example/authorize", "cid", "http://localhost:8765/callback", "st", "ch")

    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["aud"] == FHIR_BASE
    assert (q["state"], q["code_challenge"], q["code_challenge_method"]) == ("st", "ch", "S256")
    assert "launch/patient" in q["scope"].split()


def test_summary_prefers_the_official_name():
    patient = {"id": "x", "gender": "female", "birthDate": "1987-09-12",
               "name": [{"use": "usual", "text": "Cami"}, {"use": "official", "text": "Camila Maria Lopez"}]}

    assert summarize(patient)["name"] == "Camila Maria Lopez"
