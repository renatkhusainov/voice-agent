import pytest
from sqlalchemy.exc import IntegrityError

from app.models.models import Practice


PRACTICE_PAYLOAD = {
    "name": "Sunshine Dental",
    "timezone": "America/New_York",
    "phone": "+18135551234",
}


def create_practice(client, **overrides):
    response = client.post("/practices", json={**PRACTICE_PAYLOAD, **overrides})
    assert response.status_code == 201
    return response.json()["id"]


def call_payload(practice_id, **overrides):
    return {
        "practice_id": practice_id,
        "caller_number": "+18135550000",
        "started_at": "2026-09-15T10:00:00Z",
        "ended_at": "2026-09-15T10:05:00Z",
        "status": "completed",
        **overrides,
    }


# ── Health ────────────────────────────────────────────────────────────────────
def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ── Models ────────────────────────────────────────────────────────────────────
def test_create_practice_direct_db(db_session):
    practice = Practice(
        name="Test Dental",
        timezone="America/New_York",
        phone="+18135551234"
    )
    db_session.add(practice)
    db_session.commit()

    result = db_session.get(Practice, practice.id)

    assert result is not None
    assert result.name == "Test Dental"
    assert result.timezone == "America/New_York"
    assert result.phone == "+18135551234"


def test_practice_phone_is_unique(db_session):
    db_session.add(Practice(name="First",  timezone="America/New_York", phone="+18135551234"))
    db_session.commit()

    db_session.add(Practice(name="Second", timezone="America/New_York", phone="+18135551234"))

    with pytest.raises(IntegrityError):
        db_session.commit()


# ── Practices ─────────────────────────────────────────────────────────────────
def test_create_practice(client):
    response = client.post("/practices", json=PRACTICE_PAYLOAD)

    assert response.status_code == 201

    body = response.json()
    assert body["id"] is not None
    assert body["name"] == "Sunshine Dental"
    assert body["timezone"] == "America/New_York"
    assert body["phone"] == "+18135551234"


def test_create_practice_duplicate_phone(client):
    create_practice(client)

    response = client.post("/practices", json={**PRACTICE_PAYLOAD, "name": "Other Dental"})

    assert response.status_code == 409


def test_get_practice_not_found(client):
    response = client.get("/practices/99999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Practice not found"


def test_create_practice_empty_name(client):
    response = client.post("/practices", json={**PRACTICE_PAYLOAD, "name": ""})

    assert response.status_code == 422


def test_create_practice_missing_field(client):
    response = client.post("/practices", json={
        "name": "Sunshine Dental",
        "timezone": "America/New_York",
    })

    assert response.status_code == 422


@pytest.mark.parametrize("params", [{"skip": -1}, {"limit": 0}, {"limit": 101}])
def test_list_practices_invalid_pagination(client, params):
    response = client.get("/practices", params=params)

    assert response.status_code == 422


# ── Calls ─────────────────────────────────────────────────────────────────────
def test_create_and_list_calls(client):
    practice_id = create_practice(client)

    response = client.post("/calls", json=call_payload(practice_id))
    assert response.status_code == 201

    response = client.get("/calls", params={"practice_id": practice_id})
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["caller_number"] == "+18135550000"


def test_create_call_unknown_practice(client):
    response = client.post("/calls", json=call_payload(99999))

    assert response.status_code == 404


def test_create_call_ended_before_started(client):
    practice_id = create_practice(client)

    response = client.post("/calls", json=call_payload(
        practice_id, ended_at="2026-09-15T09:00:00Z",
    ))

    assert response.status_code == 422


def test_create_call_requires_timezone(client):
    practice_id = create_practice(client)

    response = client.post("/calls", json=call_payload(
        practice_id, started_at="2026-09-15T10:00:00", ended_at=None,
    ))

    assert response.status_code == 422
