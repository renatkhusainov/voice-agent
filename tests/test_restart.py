"""The restart test: kill the process mid-conversation, restart, same
session id -> slots intact.

Not a unit test in the same sense as the rest of this suite — it needs a
real, reachable Redis (`docker compose up -d redis`) and spawns two
genuinely separate OS processes (tests/_restart_helper.py, invoked twice via
`subprocess.run`), because that's the only way to actually prove state
survives a *process* restart rather than just a Python object going out of
scope. Everything else in app/agent/state.py's confirmation gate is already
covered without real infrastructure — see tests/test_state.py's mutation-
tested gate tests and tests/test_store.py's fakeredis-backed round trips.

Skipped automatically wherever Redis isn't reachable (no Docker, sockets
blocked, CI without it) rather than failing — this is a real infrastructure
dependency, and "no Redis available" is a different fact than "the restart
doesn't work."
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import redis

from app.config import settings

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = Path(__file__).resolve().parent / "_restart_helper.py"


def _redis_reachable() -> bool:
    try:
        # A fresh client with a short timeout, not store.get_client()'s
        # shared one — this only needs to answer "can we reach it right now,"
        # fast, not hang for the default connection timeout when it can't.
        redis.from_url(settings.redis_url, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


def _run_helper(mode: str) -> subprocess.CompletedProcess:
    # PYTHONPATH, not `-m`: the helper only needs app.agent.store/state
    # importable, not pytest's own conftest/package machinery — it isn't a
    # pytest test and shouldn't go through pytest fixtures at all.
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run(
        [sys.executable, str(HELPER), mode], capture_output=True, text=True, env=env, timeout=30,
    )


@pytest.mark.skipif(
    not _redis_reachable(),
    reason="Redis not reachable at settings.redis_url — start it with `docker compose up -d redis` "
           "(and REDIS_URL=redis://localhost:6379/0 if running outside Docker) to run this test.",
)
def test_dialog_state_survives_a_real_process_restart():
    write = _run_helper("write")
    assert write.returncode == 0, f"write failed: {write.stdout}\n{write.stderr}"
    assert "wrote" in write.stdout

    # Nothing connects these two subprocess.run calls except Redis itself —
    # no shared Python process, no shared module state. This is the "restart."
    read = _run_helper("read")

    try:
        assert read.returncode == 0, f"state did not survive the restart: {read.stdout}\n{read.stderr}"
        assert "MATCH" in read.stdout
    finally:
        _run_helper("cleanup")
