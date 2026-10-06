import pytest
from fastapi.testclient import TestClient
from loguru import logger

from experiments.raw_media_streams.server import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def log_messages():
    """Every loguru message the raw server emits, captured directly — this
    standalone experiment doesn't wire up app/phi/log_scrub.py (it's not
    product, and its own log lines never carry payload content to begin
    with; see test_log_output_never_contains_a_payload)."""
    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(m.record["message"]), level="DEBUG")
    yield seen
    logger.remove(handler_id)
