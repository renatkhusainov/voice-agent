import os

# ── Заглушки для настроек — до импорта app, чтобы тестам не нужен был .env ───
# Переменные окружения важнее .env, поэтому тесты никогда не видят реальные ключи
for key in (
    "TWILIO_NUMBER",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "DEEPGRAM_API_KEY",
    "ANTHROPIC_API_KEY",
):
    os.environ.setdefault(key, "test")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.main import app
from app.db import get_db
from app.models.models import Base

# ── Тестовый движок — SQLite в памяти ────────────────────────────────────────
# StaticPool: одно соединение на все потоки. Без него каждый поток
# (TestClient выполняет эндпоинты в worker-потоках) получает свою пустую БД.
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=engine)


# ── Тестовая замена get_db ────────────────────────────────────────────────────
def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="function")
def client():
    Base.metadata.create_all(engine)
    # Подменяем get_db на тестовую версию
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c

    # Чистим после теста
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)


@pytest.fixture(scope="function")
def db_session():
    """Прямой доступ к тестовой БД — для тестов моделей."""
    Base.metadata.create_all(engine)
    session = TestingSessionLocal()
    yield session
    session.rollback()
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def schedule():
    """Every test gets a fresh in-memory scheduling system (tests/scheduling_fake.py)
    instead of the FHIR server; tests/test_fhir_scheduling.py swaps in the
    real FHIR gateway against HAPI."""
    from app.scheduling import set_gateway
    from tests.scheduling_fake import InMemoryScheduleGateway

    fake = InMemoryScheduleGateway()
    set_gateway(fake)
    yield fake
    set_gateway(None)


@pytest.fixture
def log_messages():
    """Every loguru message our code emits, captured BEFORE the scrubber sees it.

    Asserting on this (rather than on scrubbed output) proves a log line never
    carried PHI to begin with, instead of merely that the scrubber caught it.
    """
    from loguru import logger

    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(m.record["message"]), level="DEBUG")
    yield seen
    logger.remove(handler_id)
