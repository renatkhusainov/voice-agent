"""app/agent/session.py: the session store both POST /agent/turn and
python -m app.agent.chat share (get_or_create_session, take_turn) — including
its integration with DialogState/the confirmation gate (app/agent/state.py)
and Redis persistence (app/agent/store.py). No real Redis needed:
fakeredis.FakeRedis() stands in, passed as take_turn's `redis_client`.
"""

import fakeredis
import pytest

from app.agent import store
from app.agent.session import get_or_create_session, reset_sessions, take_turn
from app.models.models import Lead, Practice
from app.services.calls import start_call
from tests.agent_fakes import FakeClient, text_response, tool_use_response


@pytest.fixture(autouse=True)
def _clean_sessions():
    reset_sessions()
    yield
    reset_sessions()


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


def add_practice(db_session, *, business_hours=None):
    practice = Practice(
        name="Sunshine Dental", timezone="America/New_York", phone="+18135551234",
        business_hours=business_hours,
    )
    db_session.add(practice)
    db_session.commit()
    return practice


def test_get_or_create_session_only_creates_a_call_once(db_session):
    created = []

    def make_call():
        created.append(1)
        return 42

    first = get_or_create_session("sess-1", create_call_id=make_call)
    second = get_or_create_session("sess-1", create_call_id=make_call)

    assert first is second
    assert first.call_id == 42
    assert len(created) == 1  # create_call_id ran once, not per lookup


def test_different_session_ids_get_independent_sessions(db_session):
    a = get_or_create_session("sess-a", create_call_id=lambda: 1)
    b = get_or_create_session("sess-b", create_call_id=lambda: 2)

    assert a is not b
    assert a.call_id != b.call_id


def test_reset_sessions_clears_state():
    session = get_or_create_session("sess-x", create_call_id=lambda: 1)
    reset_sessions()

    fresh = get_or_create_session("sess-x", create_call_id=lambda: 2)

    assert fresh is not session
    assert fresh.call_id == 2


def test_take_turn_appends_user_message_and_updates_history(db_session, redis):
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")
    session = get_or_create_session("sess-1", create_call_id=lambda: call.id)
    client = FakeClient([text_response("Hi there!")])

    reply = take_turn(client, db_session, session, practice=practice, message="Hello", redis_client=redis)

    assert reply == "Hi there!"
    assert session.messages[0] == {"role": "user", "content": "Hello"}
    assert session.messages[-1]["role"] == "assistant"


def test_take_turn_carries_history_across_calls(db_session, redis):
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")
    session = get_or_create_session("sess-1", create_call_id=lambda: call.id)
    client = FakeClient([text_response("First reply"), text_response("Second reply")])

    take_turn(client, db_session, session, practice=practice, message="First message", redis_client=redis)
    take_turn(client, db_session, session, practice=practice, message="Second message", redis_client=redis)

    contents = [m["content"] for m in session.messages if m["role"] == "user"]
    assert contents == ["First message", "Second message"]
    assert len(session.messages) == 4  # user, assistant, user, assistant


def test_take_turn_runs_a_real_tool_against_the_session_call(db_session, redis):
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")
    session = get_or_create_session("sess-1", create_call_id=lambda: call.id)
    client = FakeClient([
        tool_use_response("escalate_to_human", {"reason": "caller has a dental emergency"}),
        text_response("I'll have the office call you back."),
    ])

    reply = take_turn(
        client, db_session, session, practice=practice, message="This is an emergency!", redis_client=redis,
    )

    assert reply == "I'll have the office call you back."
    lead = db_session.query(Lead).filter_by(call_id=call.id).first()
    assert lead is not None
    assert lead.notes == "caller has a dental emergency"


# ── DialogState integration: persisted across turns, drives the gate ───────
def test_take_turn_persists_dialog_state_across_turns(db_session, redis):
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")
    session = get_or_create_session("sess-1", create_call_id=lambda: call.id)
    client = FakeClient([text_response("Sure, one moment.")])

    take_turn(client, db_session, session, practice=practice, message="Hi", redis_client=redis)

    state = store.get_state("sess-1", client=redis)
    assert state is not None
    assert state.turn_count == 1


def test_take_turn_blocks_booking_on_the_first_proposal(db_session, redis):
    # book_appointment writes only conflict-check against existing
    # appointments, not business hours (that's check_availability's job) —
    # so no business_hours setup is needed for this test's requested_slot.
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")
    session = get_or_create_session("sess-1", create_call_id=lambda: call.id)
    client = FakeClient([
        tool_use_response("book_appointment", {
            "practice_id": practice.id, "caller_name": "Dana Lee", "service": "cleaning",
            "callback_number": "+18135550142", "requested_slot": "2026-10-01T13:00:00Z",
        }),
        text_response("Let me confirm those details with you."),
    ])

    take_turn(client, db_session, session, practice=practice, message="Book me a cleaning", redis_client=redis)

    # No Appointment got created — the gate turned the first call into a
    # proposal, not a booking. (No DB assertion needed beyond this: if it had
    # booked, book_appointment's own tests already prove what that looks
    # like — this test is specifically about the gate refusing to.)
    from app.models.models import Appointment
    assert db_session.query(Appointment).count() == 0

    state = store.get_state("sess-1", client=redis)
    assert state.pending_confirmation is True
    assert state.confirmed is False


def test_take_turn_books_on_a_later_confirming_turn(db_session, redis):
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")
    session = get_or_create_session("sess-1", create_call_id=lambda: call.id)
    booking_args = {
        "practice_id": practice.id, "caller_name": "Dana Lee", "service": "cleaning",
        "callback_number": "+18135550142", "requested_slot": "2026-10-01T13:00:00Z",
    }
    client = FakeClient([
        tool_use_response("book_appointment", booking_args),
        text_response("Let me confirm those details with you."),
        tool_use_response("book_appointment", booking_args),
        text_response("You're all set for the cleaning!"),
    ])

    take_turn(client, db_session, session, practice=practice, message="Book me a cleaning", redis_client=redis)
    reply = take_turn(client, db_session, session, practice=practice, message="Yes, that's right", redis_client=redis)

    assert reply == "You're all set for the cleaning!"
    from app.models.models import Appointment
    assert db_session.query(Appointment).count() == 1
    appt = db_session.query(Appointment).first()
    assert appt.status.value == "pending"

    state = store.get_state("sess-1", client=redis)
    assert state.confirmed is True
    assert state.pending_confirmation is False
