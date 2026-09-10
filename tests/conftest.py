import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from fastapi.testclient import TestClient

from app.main import app
from app.db import get_db
from app.models.models import Base

# ── Тестовый движок — SQLite в памяти ────────────────────────────────────────
engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(bind=engine)


# ── Тестовая замена get_db ────────────────────────────────────────────────────
def override_get_db():
    Base.metadata.create_all(engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="function")
def client():
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