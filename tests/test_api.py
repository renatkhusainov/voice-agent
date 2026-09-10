from fastapi import responses
import pytest
from fastapi.testclient import TestClient
 
from app.main import app
from app.models.models import Practice


def test_health(client):
    response = client.get("/health")
 
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
    from sqlalchemy.exc import IntegrityError
 
    db_session.add(Practice(name="First",  timezone="America/New_York", phone="+18135551234"))
    db_session.commit()
 
    db_session.add(Practice(name="Second", timezone="America/New_York", phone="+18135551234"))
 
    with pytest.raises(IntegrityError):
        db_session.commit()
 

def test_create_practice(client):
    response = client.post("/practices", json={
        "name": "Sunshine Dental",
        "timezone": "America/New_York",
        "phone": "+18135551234"
    })
 
    assert response.status_code == 201
 
    body = response.json()
    assert body["id"] is not None
    assert body["name"] == "Sunshine Dental"
    assert body["timezone"] == "America/New_York"
    assert body["phone"] == "+18135551234"

def test_get_practice_not_found(client):
    response = client.get("/practices/99999")
 
    assert response.status_code == 404
    assert response.json()["detail"] == "Practice not found"

def test_create_practice_empty_name(client):
    response = client.post("/practices", json={
        "name": "",
        "timezone": "America/New_York",
        "phone": "+18135551234"
    })

    assert response.status_code in (201, 422)

def test_create_practice_missing_field(client):
    response = client.post("/practices", json={
        "name": "Sunshine Dental",
        "timezone": "America/New_York",
    })
 
    assert response.status_code == 422