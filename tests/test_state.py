"""app/agent/state.py: DialogState, BookingSlots, and the confirmation gate
around book_appointment — the mechanics in isolation, not through the whole
loop (tests/test_agent_session.py covers the end-to-end integration).
"""

from datetime import datetime, timezone

import pytest

from app.agent.prompts import PROMPT_VERSION
from app.agent.state import (
    BookingProposalResult,
    BookingSlots,
    DialogIntent,
    DialogState,
    build_gated_dispatch,
    gated_book_appointment,
)
from app.agent.tools import BookAppointmentInput, ToolError
from app.models.models import Appointment, Practice
from app.services.calls import start_call


def add_practice(db_session):
    practice = Practice(name="Sunshine Dental", timezone="America/New_York", phone="+18135551234")
    db_session.add(practice)
    db_session.commit()
    return practice


# ── BookingSlots ─────────────────────────────────────────────────────────────
def test_empty_slots_are_all_missing():
    slots = BookingSlots()

    assert slots.is_complete is False
    assert slots.missing_fields() == ["name", "phone", "service", "time"]


def test_complete_slots_report_nothing_missing():
    slots = BookingSlots(
        practice_id=1, caller_name="Dana", callback_number="+18135550142",
        service="cleaning", requested_slot=datetime.now(timezone.utc),
    )

    assert slots.is_complete is True
    assert slots.missing_fields() == []


# ── DialogState: defaults and prompt rendering ──────────────────────────────
def test_default_state_stamps_the_current_prompt_version():
    assert DialogState().prompt_version == PROMPT_VERSION


def test_progress_is_hidden_before_anything_is_known():
    assert DialogState().should_show_progress() is False


def test_progress_shows_once_intent_is_set():
    state = DialogState(intent=DialogIntent.book_appointment)

    assert state.should_show_progress() is True


def test_summary_shows_check_and_cross_marks():
    state = DialogState(slots=BookingSlots(caller_name="Dana Lee", service="cleaning"))

    summary = state.summary_for_prompt()

    assert summary == "Collected so far: name ✓ (Dana Lee), phone ✗, service ✓ (cleaning), time ✗."


def test_summary_also_masks_the_phone_number():
    # Masked in both places, not just read_back_text: the model doesn't need
    # the raw digits from this summary to know the field is filled, and the
    # value it actually passes to book_appointment comes from the caller's
    # own words in the conversation, not from re-reading this string.
    state = DialogState(slots=BookingSlots(callback_number="+18135550142"))

    summary = state.summary_for_prompt()

    assert "0142" in summary
    assert "+18135550142" not in summary


# ── read_back_text: last-four-digits masking ────────────────────────────────
def test_read_back_masks_phone_to_last_four_digits():
    state = DialogState(slots=BookingSlots(
        caller_name="Dana Lee", callback_number="+1 (813) 555-0142",
        service="cleaning", requested_slot=datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc),
    ))

    text = state.read_back_text()

    # Last four only, and spaced so TTS says "zero one four two", not
    # "one hundred forty-two" (app/agent/spoken.py).
    assert "ending in 0 1 4 2" in text
    assert "8135550142" not in text and "555-0142" not in text
    assert "Dana Lee" in text
    assert "cleaning" in text


def test_read_back_says_the_time_in_the_practice_timezone():
    from zoneinfo import ZoneInfo

    state = DialogState(slots=BookingSlots(
        caller_name="Dana Lee", callback_number="+18135550142", service="cleaning",
        requested_slot=datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc),  # 9 AM in New York
    ))

    text = state.read_back_text(ZoneInfo("America/New_York"))

    assert text == "Dana Lee, phone number ending in 0 1 4 2, for a cleaning at 9 AM Thursday the 1st"
    assert "13:00" not in text and "+00:00" not in text


def test_read_back_reports_missing_fields_plainly():
    text = DialogState().read_back_text()

    assert text.count("missing") == 4


# ── The confirmation gate itself ────────────────────────────────────────────
def _payload(practice_id, slot="2026-10-01T13:00:00+00:00"):
    return BookAppointmentInput(
        practice_id=practice_id, caller_name="Dana Lee", callback_number="+18135550142",
        service="cleaning", requested_slot=slot,
    )


def test_first_call_proposes_and_does_not_book(db_session):
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")
    state = DialogState(turn_count=1)

    result = gated_book_appointment(db_session, _payload(practice.id), call_id=call.id, state=state)

    assert isinstance(result, BookingProposalResult)
    assert result.status == "pending_confirmation"
    assert db_session.query(Appointment).count() == 0
    assert state.pending_confirmation is True
    assert state.confirmed is False
    assert state.proposed_at_turn == 1


def test_second_call_same_turn_does_not_book(db_session):
    # No intervening take_turn (no turn_count advance) -- the model calling
    # the tool twice within the same batch must not be able to self-confirm.
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")
    state = DialogState(turn_count=1)

    gated_book_appointment(db_session, _payload(practice.id), call_id=call.id, state=state)
    result = gated_book_appointment(db_session, _payload(practice.id), call_id=call.id, state=state)

    assert isinstance(result, BookingProposalResult)
    assert db_session.query(Appointment).count() == 0


def test_matching_call_on_a_later_turn_books(db_session):
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")
    state = DialogState(turn_count=1)

    gated_book_appointment(db_session, _payload(practice.id), call_id=call.id, state=state)
    state.turn_count = 2  # a real take_turn call would do this
    result = gated_book_appointment(db_session, _payload(practice.id), call_id=call.id, state=state)

    assert not isinstance(result, BookingProposalResult)
    assert result.status.value == "pending"
    assert db_session.query(Appointment).count() == 1
    assert state.confirmed is True
    assert state.pending_confirmation is False


def test_a_changed_detail_on_the_later_turn_re_proposes_instead_of_booking(db_session):
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")
    state = DialogState(turn_count=1)

    gated_book_appointment(db_session, _payload(practice.id), call_id=call.id, state=state)
    state.turn_count = 2
    changed = _payload(practice.id, slot="2026-10-01T14:00:00+00:00")  # different time
    result = gated_book_appointment(db_session, changed, call_id=call.id, state=state)

    assert isinstance(result, BookingProposalResult)
    assert db_session.query(Appointment).count() == 0
    assert state.proposed_at_turn == 2  # re-proposed on this turn, not the earlier one


def test_gate_still_raises_tool_error_for_a_genuinely_bad_confirmed_booking(db_session):
    # The gate defers to book_appointment's own checks once confirmed --
    # doesn't swallow a real ToolError (e.g. the slot got taken in between).
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")
    other_call = start_call(db_session, practice.id, "+18135559999")
    state = DialogState(turn_count=1)

    gated_book_appointment(db_session, _payload(practice.id), call_id=call.id, state=state)
    state.turn_count = 2
    # Someone else's tool call books the exact same slot in between.
    from app.agent.tools import book_appointment as real_book_appointment
    real_book_appointment(db_session, _payload(practice.id), call_id=other_call.id)

    with pytest.raises(ToolError):
        gated_book_appointment(db_session, _payload(practice.id), call_id=call.id, state=state)


# ── build_gated_dispatch ─────────────────────────────────────────────────────
def test_build_gated_dispatch_has_all_five_tools():
    state = DialogState()
    dispatch = build_gated_dispatch(call_id=1, state=state)

    assert set(dispatch) == {
        "check_availability", "book_appointment", "reschedule_appointment",
        "escalate_to_human", "answer_faq",
    }


def test_build_gated_dispatch_book_appointment_is_the_gated_version(db_session):
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")
    state = DialogState(turn_count=1)
    dispatch = build_gated_dispatch(call.id, state)

    result = dispatch["book_appointment"].call(db_session, _payload(practice.id))

    assert isinstance(result, BookingProposalResult)  # not a real booking on the first call


def test_check_availability_writes_practice_id_into_state(db_session):
    practice = add_practice(db_session)
    state = DialogState()
    dispatch = build_gated_dispatch(call_id=1, state=state)
    from app.agent.tools import CheckAvailabilityInput, DateRange
    payload = CheckAvailabilityInput(
        practice_id=practice.id,
        date_range=DateRange(start_date="2026-10-01", end_date="2026-10-01"),
    )

    dispatch["check_availability"].call(db_session, payload)

    assert state.slots.practice_id == practice.id
    assert state.intent == DialogIntent.check_availability
