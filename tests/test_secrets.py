import os
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.db import get_db
from app.main import app
from app.phi.log_scrub import MIN_SECRET_LEN, _SECRET_ENV_NAME

ROOT = Path(__file__).resolve().parent.parent


# ── errors returned to a client ────────────────────────────────────────────────
def test_unhandled_error_response_carries_no_detail():
    def broken_db():
        raise RuntimeError("connect failed: password=hunter2-secret-value key=abc123secret")

    app.dependency_overrides[get_db] = broken_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/practices/1")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert response.text == "Internal Server Error"


def test_settings_validation_errors_do_not_echo_the_offending_value():
    with pytest.raises(ValidationError) as raised:
        Settings(
            _env_file=None,
            database_url="sqlite://",
            twilio_number="n",
            twilio_account_sid="s",
            twilio_auth_token="t",
            deepgram_api_key="d",
            anthropic_api_key=["SUPER-SECRET-VALUE"],  # wrong type on purpose
        )

    assert "SUPER-SECRET-VALUE" not in str(raised.value)


# ── the repo ───────────────────────────────────────────────────────────────────
def _tracked_files():
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True)
    if result.returncode != 0:
        pytest.skip("not a git checkout")
    return [p for p in result.stdout.decode().split("\0") if p]


def test_env_file_is_not_tracked():
    assert ".env" not in _tracked_files()


def test_no_credential_is_committed():
    """Neither a live secret from this machine's environment, nor anything
    shaped like an Anthropic key or a Twilio SID, appears in a tracked file."""
    live = {
        name: value
        for name, value in os.environ.items()
        if _SECRET_ENV_NAME.search(name) and len(value) >= MIN_SECRET_LEN
    }
    shapes = [re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), re.compile(r"\b(?:AC|SK)[0-9a-f]{32}\b")]

    leaked = []  # (path, why): names only, so a failure never prints a secret
    for rel in _tracked_files():
        if rel.endswith(("uv.lock", "package-lock.json")):
            continue
        raw = (ROOT / rel).read_bytes()
        if b"\0" in raw or len(raw) > 1_000_000:
            continue
        text = raw.decode("utf-8", errors="ignore")
        leaked += [(rel, f"value of ${name}") for name, value in live.items() if value in text]
        leaked += [(rel, "key-shaped string") for shape in shapes if shape.search(text)]

    assert not leaked, f"credentials in tracked files: {leaked}"
