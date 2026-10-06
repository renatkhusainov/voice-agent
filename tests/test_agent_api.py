"""POST /agent/turn — text in, text out, conversation kept per session_id.

Runs against the real app, the real router, the real agent loop and tools —
only the Anthropic client and the Redis client are faked, via the same
dependency-override pattern `client`/`get_db` already use in
tests/conftest.py, so this needs no network, API key, or a real Redis.
"""

import fakeredis
import pytest

from app.agent.prompts import PROMPT_VERSION
from app.agent.session import reset_sessions
from app.config import settings
from app.main import app
from app.models.models import Appointment, Call, CallStatus, Lead, Practice
from app.routers.agent import get_anthropic_client, get_redis_client
from tests.agent_fakes import FakeClient, text_response, tool_use_response


@pytest.fixture(autouse=True)
def _clean_sessions():
    reset_sessions()
    # One fake Redis instance per test, constructed once and returned as-is
    # on every dependency resolution — not `lambda: fakeredis.FakeRedis()`,
    # which would hand back a fresh, empty store on every single request.
    # DialogState needs to actually persist between two POSTs to the same
    # session_id, the same way real Redis would.
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    app.dependency_overrides[get_redis_client] = lambda: fake_redis
    yield
    reset_sessions()
    app.dependency_overrides.pop(get_anthropic_client, None)
    app.dependency_overrides.pop(get_redis_client, None)


def add_practice(db_session, *, phone="+18135551234"):
    practice = Practice(name="Sunshine Dental", timezone="America/New_York", phone=phone)
    db_session.add(practice)
    db_session.commit()
    return practice


def use_fake_client(*responses):
    fake = FakeClient(list(responses))
    app.dependency_overrides[get_anthropic_client] = lambda: fake
    return fake


def test_unknown_practice_404s(client):
    use_fake_client(text_response("hi"))

    response = client.post("/agent/turn", json={
        "session_id": "s1", "practice_id": 99999, "message": "hello",
    })

    assert response.status_code == 404


def test_turn_returns_the_reply_and_prompt_version(client, db_session):
    practice = add_practice(db_session)
    use_fake_client(text_response("Sure, when works for you?"))

    response = client.post("/agent/turn", json={
        "session_id": "s1", "practice_id": practice.id, "message": "I'd like to book a cleaning",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "Sure, when works for you?"
    assert body["prompt_version"] == PROMPT_VERSION
    assert body["session_id"] == "s1"
    assert isinstance(body["call_id"], int)


def test_first_turn_creates_a_call_row(client, db_session):
    practice = add_practice(db_session)
    use_fake_client(text_response("Hi!"))

    response = client.post("/agent/turn", json={
        "session_id": "s1", "practice_id": practice.id, "message": "hello",
    })

    call = db_session.get(Call, response.json()["call_id"])
    assert call is not None
    assert call.practice_id == practice.id
    assert call.caller_number == "agent-harness"
    assert call.status == CallStatus.in_progress


def test_same_session_id_reuses_the_call_and_keeps_history(client, db_session):
    practice = add_practice(db_session)
    fake = use_fake_client(text_response("First reply"), text_response("Second reply"))

    first = client.post("/agent/turn", json={
        "session_id": "s1", "practice_id": practice.id, "message": "First message",
    })
    second = client.post("/agent/turn", json={
        "session_id": "s1", "practice_id": practice.id, "message": "Second message",
    })

    assert first.json()["call_id"] == second.json()["call_id"]
    # The second request to the model carries the full conversation so far.
    second_call_messages = fake.messages.calls[1]["messages"]
    user_texts = [m["content"] for m in second_call_messages if m["role"] == "user"]
    assert user_texts == ["First message", "Second message"]


def test_different_session_ids_get_different_calls(client, db_session):
    practice = add_practice(db_session)
    use_fake_client(text_response("a"), text_response("b"))

    first = client.post("/agent/turn", json={
        "session_id": "s1", "practice_id": practice.id, "message": "hi",
    })
    second = client.post("/agent/turn", json={
        "session_id": "s2", "practice_id": practice.id, "message": "hi",
    })

    assert first.json()["call_id"] != second.json()["call_id"]


def test_turn_that_calls_a_tool_actually_runs_it(client, db_session):
    practice = add_practice(db_session)
    use_fake_client(
        tool_use_response("escalate_to_human", {"reason": "dental emergency"}),
        text_response("I'll have the office call you back."),
    )

    response = client.post("/agent/turn", json={
        "session_id": "s1", "practice_id": practice.id, "message": "This is an emergency!",
    })

    assert response.json()["reply"] == "I'll have the office call you back."
    lead = db_session.query(Lead).filter_by(call_id=response.json()["call_id"]).first()
    assert lead is not None and lead.notes == "dental emergency"


@pytest.mark.parametrize("payload", [
    {"practice_id": 1, "message": "hi"},  # missing session_id
    {"session_id": "s1", "message": "hi"},  # missing practice_id
    {"session_id": "s1", "practice_id": 1},  # missing message
    {"session_id": "", "practice_id": 1, "message": "hi"},  # empty session_id
    {"session_id": "s1", "practice_id": 1, "message": ""},  # empty message
])
def test_invalid_request_bodies_422(client, payload):
    response = client.post("/agent/turn", json=payload)

    assert response.status_code == 422


# ── DialogState / confirmation gate, end to end over HTTP ──────────────────
def test_confirmation_gate_over_two_http_turns(client, db_session):
    practice = add_practice(db_session)
    booking_args = {
        "practice_id": practice.id, "caller_name": "Dana Lee", "service": "cleaning",
        "callback_number": "+18135550142", "requested_slot": "2030-10-01T13:00:00Z",
    }
    use_fake_client(
        tool_use_response("book_appointment", booking_args),
        text_response("Let me confirm those details with you."),
        tool_use_response("book_appointment", booking_args),
        text_response("You're all set!"),
    )

    first = client.post("/agent/turn", json={
        "session_id": "s1", "practice_id": practice.id, "message": "Book me a cleaning",
    })
    assert first.json()["reply"] == "Let me confirm those details with you."
    assert db_session.query(Appointment).count() == 0  # not booked on the proposal turn

    second = client.post("/agent/turn", json={
        "session_id": "s1", "practice_id": practice.id, "message": "Yes, that's right",
    })
    assert second.json()["reply"] == "You're all set!"
    assert db_session.query(Appointment).count() == 1


# ── GET /agent/sessions/{id} — dev-only debug endpoint ──────────────────────
@pytest.fixture
def _dev_environment(monkeypatch):
    """The endpoint's own default is "disabled" (settings.environment ==
    "production", see app/config.py) — most tests need it explicitly opted
    in, the same way an operator would set ENVIRONMENT=development locally."""
    monkeypatch.setattr(settings, "environment", "development")


def test_settings_default_environment_is_production_not_development():
    # A pure class-default check, deliberately independent of whatever
    # ENVIRONMENT happens to be set to on this machine right now: proves the
    # compiled-in default is the safe (disabled) one, not just that some
    # ambient env var happens to disable it today.
    from app.config import Settings
    assert Settings.model_fields["environment"].default == "production"


def test_session_state_endpoint_404s_when_not_development(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    practice = add_practice(db_session)
    use_fake_client(text_response("hi"))
    client.post("/agent/turn", json={"session_id": "s1", "practice_id": practice.id, "message": "hi"})

    response = client.get("/agent/sessions/s1")

    assert response.status_code == 404


def test_session_state_endpoint_returns_dialog_state(client, db_session, _dev_environment):
    practice = add_practice(db_session)
    use_fake_client(text_response("Sure, when works for you?"))
    client.post("/agent/turn", json={
        "session_id": "s1", "practice_id": practice.id, "message": "I'd like to book a cleaning",
    })

    response = client.get("/agent/sessions/s1")

    assert response.status_code == 200
    body = response.json()
    assert body["turn_count"] == 1
    assert body["prompt_version"] == PROMPT_VERSION
    assert body["pending_confirmation"] is False
    assert "slots" in body


def test_session_state_endpoint_reflects_pending_confirmation(client, db_session, _dev_environment):
    practice = add_practice(db_session)
    booking_args = {
        "practice_id": practice.id, "caller_name": "Dana Lee", "service": "cleaning",
        "callback_number": "+18135550142", "requested_slot": "2030-10-01T13:00:00Z",
    }
    use_fake_client(
        tool_use_response("book_appointment", booking_args),
        text_response("Let me confirm those details with you."),
    )
    client.post("/agent/turn", json={
        "session_id": "s1", "practice_id": practice.id, "message": "Book me a cleaning",
    })

    body = client.get("/agent/sessions/s1").json()

    assert body["pending_confirmation"] is True
    assert body["slots"]["caller_name"] == "Dana Lee"
    assert body["slots"]["service"] == "cleaning"
    # Phone is NOT masked here — this is a raw debug dump of the real state,
    # unlike summary_for_prompt()/read_back_text() (app/agent/state.py),
    # which mask it on purpose for what the model sees and says.
    assert body["slots"]["callback_number"] == "+18135550142"


def test_session_state_endpoint_404s_for_unknown_session(client, _dev_environment):
    response = client.get("/agent/sessions/no-such-session")

    assert response.status_code == 404


# ── The canonical proof: booking cannot execute without a confirmed state ──
def test_booking_cannot_execute_without_a_confirmed_state(client, db_session, _dev_environment):
    """Definition of Done, stated directly: the model calling book_appointment
    is not sufficient to create an Appointment. Checked two independent ways
    — the database has no row, and DialogState (via the debug endpoint) says
    why: pending_confirmation, not confirmed — so this isn't "no error was
    raised," it's "the system's own record agrees nothing was booked."
    """
    practice = add_practice(db_session)
    use_fake_client(tool_use_response("book_appointment", {
        "practice_id": practice.id, "caller_name": "Dana Lee", "service": "cleaning",
        "callback_number": "+18135550142", "requested_slot": "2030-10-01T13:00:00Z",
    }), text_response("Let me just confirm those details with you first."))

    client.post("/agent/turn", json={
        "session_id": "s1", "practice_id": practice.id, "message": "Book me a cleaning next week",
    })

    # 1. The database: no Appointment exists.
    assert db_session.query(Appointment).count() == 0

    # 2. The state machine's own record agrees, and says why.
    state = client.get("/agent/sessions/s1").json()
    assert state["pending_confirmation"] is True
    assert state["confirmed"] is False
