"""app/agent/tools.py: the five tools, exercised against a real (in-memory)
database — not mocked — plus a check that every advertised schema really
does come from its model, not a hand-typed duplicate.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.agent.schema import schema_from_model
from app.agent.tools import (
    ANSWER_FAQ_SCHEMA,
    BOOK_APPOINTMENT_SCHEMA,
    CHECK_AVAILABILITY_SCHEMA,
    ESCALATE_TO_HUMAN_SCHEMA,
    ESCALATION_MESSAGE,
    RESCHEDULE_APPOINTMENT_SCHEMA,
    TOOLS,
    AnswerFaqInput,
    BookAppointmentInput,
    CheckAvailabilityInput,
    DateRange,
    EscalateToHumanInput,
    RescheduleAppointmentInput,
    ToolError,
    answer_faq,
    book_appointment,
    cancel_appointment,
    check_availability,
    escalate_to_human,
    reschedule_appointment,
)
from app.models.models import Appointment, AppointmentStatus, Intent, Lead, Practice
from app.services.calls import start_call


def add_practice(db_session, *, phone="+18135551234", timezone="America/New_York", business_hours=None):
    practice = Practice(name="Sunshine Dental", timezone=timezone, phone=phone, business_hours=business_hours)
    db_session.add(practice)
    db_session.commit()
    return practice


def add_call(db_session, practice):
    return start_call(db_session, practice.id, "+18135550000")


def next_weekday(target: int, *, from_day: date | None = None) -> date:
    """The next date (strictly after from_day/today) whose weekday() == target
    (Monday=0 ... Sunday=6) — deterministic regardless of when tests run."""
    start = from_day or date.today()
    ahead = (target - start.weekday()) % 7
    return start + timedelta(days=ahead or 7)


def open_slot(n: int) -> datetime:
    """The n-th weekday from tomorrow at 10:00 AM New York time, as UTC: a
    real, bookable slot under DEFAULT_BUSINESS_HOURS for add_practice()'s
    default timezone. book_appointment now refuses times the practice
    doesn't offer, so tests can't book "now + 1 day" anymore."""
    day, found = date.today(), 0
    while found < n:
        day += timedelta(days=1)
        found += day.weekday() < 5
    return datetime(day.year, day.month, day.day, 10, 0, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


# ── 1. check_availability ────────────────────────────────────────────────────
def test_check_availability_unknown_practice_raises(db_session):
    with pytest.raises(ToolError):
        check_availability(db_session, CheckAvailabilityInput(
            practice_id=99999, date_range=DateRange(start_date=date.today(), end_date=date.today()),
        ))


def test_check_availability_generates_slots_within_a_one_hour_window(db_session):
    day = date.today() + timedelta(days=30)
    business_hours = {day.strftime("%a").lower(): ["09:00", "10:00"]}
    practice = add_practice(db_session, timezone="UTC", business_hours=business_hours)

    result = check_availability(db_session, CheckAvailabilityInput(
        practice_id=practice.id, date_range=DateRange(start_date=day, end_date=day),
    ))

    assert [s.start for s in result.slots] == [
        datetime(day.year, day.month, day.day, 9, 0, tzinfo=timezone.utc),
        datetime(day.year, day.month, day.day, 9, 30, tzinfo=timezone.utc),
    ]
    assert result.slots[0].end == result.slots[1].start


def test_check_availability_converts_local_business_hours_to_utc(db_session):
    day = date.today() + timedelta(days=30)
    business_hours = {day.strftime("%a").lower(): ["09:00", "09:30"]}
    # America/New_York: UTC-4 (EDT) or UTC-5 (EST) depending on the date — either
    # way, 9am local must land at a different UTC hour than a UTC practice would.
    practice = add_practice(db_session, timezone="America/New_York", business_hours=business_hours)

    result = check_availability(db_session, CheckAvailabilityInput(
        practice_id=practice.id, date_range=DateRange(start_date=day, end_date=day),
    ))

    [slot] = result.slots
    assert slot.start.tzinfo is not None
    assert slot.start.hour in (13, 14)  # 9am ET -> 13:00 or 14:00 UTC (DST-dependent)
    assert slot.start.hour != 9  # sanity: this is not just UTC business hours


def test_check_availability_slots_carry_a_spoken_local_time(db_session):
    # The module's own test phrase: "9:30 AM Tuesday the 15th". 2030-01-15 is
    # a Tuesday; 9:30 AM in New York (EST, UTC-5) is 14:30 UTC.
    day = date(2030, 1, 15)
    if day <= date.today():
        pytest.skip("fixed date is in the past; slots before now are filtered out")
    practice = add_practice(db_session, business_hours={"tue": ["09:30", "10:00"]})

    [slot] = check_availability(db_session, CheckAvailabilityInput(
        practice_id=practice.id, date_range=DateRange(start_date=day, end_date=day),
    )).slots

    assert slot.start == datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc)
    assert slot.spoken == "9:30 AM Tuesday the 15th"


def test_check_availability_spoken_time_is_local_not_utc(db_session):
    day = date.today() + timedelta(days=30)
    business_hours = {day.strftime("%a").lower(): ["09:30", "10:00"]}
    practice = add_practice(db_session, timezone="America/New_York", business_hours=business_hours)

    [slot] = check_availability(db_session, CheckAvailabilityInput(
        practice_id=practice.id, date_range=DateRange(start_date=day, end_date=day),
    )).slots

    # The spoken form is the practice's own 9:30, even though `start` is 13:30/14:30 UTC.
    assert slot.spoken.startswith("9:30 AM ")
    assert slot.start.hour != 9


def test_check_availability_closed_day_has_no_slots(db_session):
    sunday = next_weekday(6)  # closed under DEFAULT_BUSINESS_HOURS
    practice = add_practice(db_session, business_hours=None)

    result = check_availability(db_session, CheckAvailabilityInput(
        practice_id=practice.id, date_range=DateRange(start_date=sunday, end_date=sunday),
    ))

    assert result.slots == []


def test_check_availability_default_hours_apply_when_unset(db_session):
    monday = next_weekday(0)
    practice = add_practice(db_session, timezone="UTC", business_hours=None)

    result = check_availability(db_session, CheckAvailabilityInput(
        practice_id=practice.id, date_range=DateRange(start_date=monday, end_date=monday),
    ))

    # At most three options, the earliest first (one day: 9:00, 9:30, 10:00).
    assert [s.start.hour * 60 + s.start.minute for s in result.slots] == [540, 570, 600]


def test_check_availability_excludes_an_already_booked_slot(db_session, schedule):
    day = date.today() + timedelta(days=30)
    business_hours = {day.strftime("%a").lower(): ["09:00", "10:00"]}
    practice = add_practice(db_session, timezone="UTC", business_hours=business_hours)
    booked_start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=timezone.utc)
    # Booked at the front desk: busy in the scheduling system, no row of ours.
    schedule.mark_busy(practice, booked_start)

    result = check_availability(db_session, CheckAvailabilityInput(
        practice_id=practice.id, date_range=DateRange(start_date=day, end_date=day),
    ))

    assert booked_start not in [s.start for s in result.slots]
    assert len(result.slots) == 1  # the 9:00 slot survives; 9:30 doesn't


def test_check_availability_excludes_slots_entirely_in_the_past(db_session):
    yesterday = date.today() - timedelta(days=1)
    business_hours = {yesterday.strftime("%a").lower(): ["00:00", "23:30"]}
    practice = add_practice(db_session, timezone="UTC", business_hours=business_hours)

    result = check_availability(db_session, CheckAvailabilityInput(
        practice_id=practice.id, date_range=DateRange(start_date=yesterday, end_date=yesterday),
    ))

    assert result.slots == []


def test_date_range_rejects_end_before_start():
    with pytest.raises(ValueError):
        DateRange(start_date=date(2026, 1, 10), end_date=date(2026, 1, 1))


def test_book_appointment_reads_a_slot_without_offset_as_practice_local_time(db_session):
    # No offset is not ambiguous here: it's defined as the practice's own
    # local time (see tools.localize_slot). Stored as UTC, like every column.
    practice = add_practice(db_session, timezone="America/New_York")
    call = add_call(db_session, practice)

    result = book_appointment(db_session, BookAppointmentInput(
        practice_id=practice.id, caller_name="Dana", service="cleaning", callback_number="+18135550142",
        requested_slot=datetime(2030, 1, 15, 9, 30),  # 9:30 AM in New York
    ), call_id=call.id)

    assert result.requested_slot == datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc)
    assert result.spoken_time == "9:30 AM Tuesday the 15th"


def test_book_appointment_treats_local_and_utc_forms_of_the_same_time_as_one_slot(db_session):
    practice = add_practice(db_session, timezone="America/New_York")
    call = add_call(db_session, practice)
    book_appointment(db_session, BookAppointmentInput(
        practice_id=practice.id, caller_name="Dana", service="cleaning", callback_number="+18135550142",
        requested_slot=datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc),
    ), call_id=call.id)

    with pytest.raises(ToolError, match="9:30 AM Tuesday the 15th is already booked"):
        book_appointment(db_session, BookAppointmentInput(
            practice_id=practice.id, caller_name="Sam", service="checkup", callback_number="+18135550199",
            requested_slot=datetime(2030, 1, 15, 9, 30),
        ), call_id=add_call(db_session, practice).id)


# ── 2. book_appointment ──────────────────────────────────────────────────────
def test_book_appointment_unknown_call_raises(db_session):
    practice = add_practice(db_session)
    with pytest.raises(ToolError):
        book_appointment(db_session, BookAppointmentInput(
            practice_id=practice.id, caller_name="Dana", service="cleaning", callback_number="+18135550142",
            requested_slot=open_slot(1),
        ), call_id=99999)


def test_book_appointment_rejects_practice_id_mismatch(db_session):
    practice = add_practice(db_session)
    other_practice = add_practice(db_session, phone="+18135559999")
    call = add_call(db_session, practice)  # belongs to `practice`, not `other_practice`

    with pytest.raises(ToolError):
        book_appointment(db_session, BookAppointmentInput(
            practice_id=other_practice.id, caller_name="Dana", service="cleaning", callback_number="+18135550142",
            requested_slot=open_slot(1),
        ), call_id=call.id)


def test_book_appointment_creates_lead_and_pending_appointment(db_session):
    practice = add_practice(db_session)
    call = add_call(db_session, practice)
    slot = open_slot(1)

    result = book_appointment(db_session, BookAppointmentInput(
        practice_id=practice.id, caller_name="Dana", service="cleaning", callback_number="+18135550142",
        requested_slot=slot, notes="prefers mornings",
    ), call_id=call.id)

    assert result.status == AppointmentStatus.confirmed
    assert result.requested_slot == slot
    assert " AM " in result.spoken_time or " PM " in result.spoken_time
    lead = db_session.get(Lead, result.lead_id)
    assert lead.call_id == call.id
    assert lead.name == "Dana"
    assert lead.callback_number == "+18135550142"
    assert lead.intent == Intent.appointment
    # service has no first-class column yet (see book_appointment's own
    # docstring) — folded into notes alongside whatever the caller added.
    assert lead.notes == "Service: cleaning\nprefers mornings"


def test_book_appointment_reuses_the_calls_existing_lead(db_session):
    practice = add_practice(db_session)
    call = add_call(db_session, practice)

    first = book_appointment(db_session, BookAppointmentInput(
        practice_id=practice.id, caller_name="Dana", service="cleaning", callback_number="+18135550142",
        requested_slot=open_slot(1),
    ), call_id=call.id)
    second = book_appointment(db_session, BookAppointmentInput(
        practice_id=practice.id, caller_name="Dana", service="cleaning", callback_number="+18135550142",
        requested_slot=open_slot(2),
    ), call_id=call.id)

    assert first.lead_id == second.lead_id
    lead_count = db_session.query(Lead).filter_by(call_id=call.id).count()
    assert lead_count == 1


def test_book_appointment_rejects_a_double_booked_slot(db_session):
    practice = add_practice(db_session)
    call1 = add_call(db_session, practice)
    call2 = add_call(db_session, practice)
    slot = open_slot(1)

    book_appointment(db_session, BookAppointmentInput(
        practice_id=practice.id, caller_name="Dana", service="cleaning", callback_number="+18135550142", requested_slot=slot,
    ), call_id=call1.id)

    with pytest.raises(ToolError):
        book_appointment(db_session, BookAppointmentInput(
            practice_id=practice.id, caller_name="Alex", service="cleaning", callback_number="+18135559999", requested_slot=slot,
        ), call_id=call2.id)


# ── 3. reschedule_appointment ────────────────────────────────────────────────
def test_reschedule_appointment_unknown_id_raises(db_session):
    with pytest.raises(ToolError):
        reschedule_appointment(db_session, RescheduleAppointmentInput(
            appointment_id=99999, new_slot=open_slot(1),
        ))


def test_reschedule_appointment_moves_to_a_new_row_and_closes_the_old_one(db_session):
    practice = add_practice(db_session)
    call = add_call(db_session, practice)
    booked = book_appointment(db_session, BookAppointmentInput(
        practice_id=practice.id, caller_name="Dana", service="cleaning", callback_number="+18135550142",
        requested_slot=open_slot(1),
    ), call_id=call.id)
    new_slot = open_slot(2)

    result = reschedule_appointment(db_session, RescheduleAppointmentInput(
        appointment_id=booked.appointment_id, new_slot=new_slot, reason="conflict came up",
    ))

    assert result.previous_appointment_id == booked.appointment_id
    assert result.appointment_id != booked.appointment_id
    assert result.status == AppointmentStatus.confirmed
    assert result.requested_slot == new_slot

    old = db_session.get(Appointment, booked.appointment_id)
    assert old.status == AppointmentStatus.rescheduled
    lead = db_session.get(Lead, booked.lead_id)
    assert "conflict came up" in lead.notes


def test_reschedule_appointment_rejects_already_rescheduled(db_session):
    practice = add_practice(db_session)
    call = add_call(db_session, practice)
    booked = book_appointment(db_session, BookAppointmentInput(
        practice_id=practice.id, caller_name="Dana", service="cleaning", callback_number="+18135550142",
        requested_slot=open_slot(1),
    ), call_id=call.id)
    reschedule_appointment(db_session, RescheduleAppointmentInput(
        appointment_id=booked.appointment_id, new_slot=open_slot(2),
    ))

    with pytest.raises(ToolError):
        reschedule_appointment(db_session, RescheduleAppointmentInput(
            appointment_id=booked.appointment_id, new_slot=open_slot(3),
        ))


def test_reschedule_appointment_rejects_a_conflicting_new_slot(db_session):
    practice = add_practice(db_session)
    call1 = add_call(db_session, practice)
    call2 = add_call(db_session, practice)
    taken_slot = open_slot(5)
    book_appointment(db_session, BookAppointmentInput(
        practice_id=practice.id, caller_name="Alex", service="cleaning", callback_number="+18135559999", requested_slot=taken_slot,
    ), call_id=call1.id)
    to_move = book_appointment(db_session, BookAppointmentInput(
        practice_id=practice.id, caller_name="Dana", service="cleaning", callback_number="+18135550142",
        requested_slot=open_slot(1),
    ), call_id=call2.id)

    with pytest.raises(ToolError):
        reschedule_appointment(db_session, RescheduleAppointmentInput(
            appointment_id=to_move.appointment_id, new_slot=taken_slot,
        ))


# ── 4. escalate_to_human ─────────────────────────────────────────────────────
def test_escalate_to_human_unknown_call_raises(db_session):
    with pytest.raises(ToolError):
        escalate_to_human(db_session, EscalateToHumanInput(reason="needs a dentist's judgment"), call_id=99999)


def test_escalate_to_human_records_reason_and_returns_fixed_message(db_session):
    practice = add_practice(db_session)
    call = add_call(db_session, practice)

    result = escalate_to_human(
        db_session, EscalateToHumanInput(reason="caller describes a dental emergency"), call_id=call.id,
    )

    assert result.message_for_caller == ESCALATION_MESSAGE
    lead = db_session.get(Lead, result.lead_id)
    assert lead.call_id == call.id
    assert lead.intent == Intent.callback
    assert lead.notes == "caller describes a dental emergency"


def test_escalation_message_gives_no_clinical_advice():
    # The handoff line is spoken verbatim on a live call (app/agent/live.py),
    # so it's the one piece of escalation wording the model can't change.
    import re

    lowered = ESCALATION_MESSAGE.lower()
    for advice in ("rinse", "ice", "compress", "pressure", "ibuprofen", "tylenol",
                   "painkiller", "sounds like", "probably", "normal", "serious"):
        assert not re.search(rf"\b{advice}\b", lowered), advice
    assert "call you back" in lowered
    assert "911" in ESCALATION_MESSAGE


def test_escalate_to_human_twice_appends_rather_than_overwrites(db_session):
    practice = add_practice(db_session)
    call = add_call(db_session, practice)

    first = escalate_to_human(db_session, EscalateToHumanInput(reason="first reason"), call_id=call.id)
    second = escalate_to_human(db_session, EscalateToHumanInput(reason="second reason"), call_id=call.id)

    assert first.lead_id == second.lead_id
    lead = db_session.get(Lead, second.lead_id)
    assert "first reason" in lead.notes
    assert "second reason" in lead.notes


# ── 5. answer_faq (stub) ─────────────────────────────────────────────────────
def test_answer_faq_always_defers_for_now():
    result = answer_faq(AnswerFaqInput(question="Do you take Delta Dental?"))

    assert result.question == "Do you take Delta Dental?"
    assert result.answered is False
    assert "call you back" in result.answer


# ── Schemas: generated from the models, matching every tool ─────────────────
@pytest.mark.parametrize("schema, model", [
    (CHECK_AVAILABILITY_SCHEMA, CheckAvailabilityInput),
    (BOOK_APPOINTMENT_SCHEMA, BookAppointmentInput),
    (RESCHEDULE_APPOINTMENT_SCHEMA, RescheduleAppointmentInput),
    (ESCALATE_TO_HUMAN_SCHEMA, EscalateToHumanInput),
    (ANSWER_FAQ_SCHEMA, AnswerFaqInput),
])
def test_each_tool_schema_is_freshly_derived_from_its_model(schema, model):
    properties, required = schema_from_model(model)

    assert schema.properties == properties
    assert schema.required == required


def test_escalate_to_human_and_answer_faq_schemas_have_exactly_the_named_field():
    # The task names these with a single parameter each — confirm the schema
    # doesn't quietly carry anything else (like a call_id) alongside it.
    assert set(ESCALATE_TO_HUMAN_SCHEMA.properties) == {"reason"}
    assert set(ANSWER_FAQ_SCHEMA.properties) == {"question"}


def test_tools_registry_has_all_five_by_name():
    assert [s.name for s in TOOLS.standard_tools] == [
        "check_availability", "book_appointment", "reschedule_appointment",
        "escalate_to_human", "answer_faq",
    ]


# ── Slots the practice doesn't offer are refused, not booked ────────────────
def _booking(practice, slot):
    return BookAppointmentInput(
        practice_id=practice.id, caller_name="Dana", service="cleaning",
        callback_number="+18135550142", requested_slot=slot,
    )


@pytest.mark.parametrize("slot, reason", [
    (datetime(2030, 1, 13, 15, 0), "isn't an appointment time"),   # Sunday 3 PM: closed
    (datetime(2030, 1, 15, 3, 0), "isn't an appointment time"),    # Tuesday 3 AM: before hours
    (datetime(2030, 1, 15, 17, 0), "isn't an appointment time"),   # 5 PM: closing time, no slot starts then
    (datetime(2030, 1, 15, 10, 10), "isn't an appointment time"),  # off the 30-minute grid
    (datetime(2020, 1, 14, 10, 0), "has already passed"),          # a Tuesday, in the past
])
def test_book_appointment_refuses_a_slot_the_practice_does_not_offer(db_session, slot, reason):
    practice = add_practice(db_session)  # America/New_York, default Mon–Fri 9–5
    call = add_call(db_session, practice)

    with pytest.raises(ToolError, match=reason):
        book_appointment(db_session, _booking(practice, slot), call_id=call.id)

    assert db_session.query(Appointment).count() == 0


def test_reschedule_refuses_a_slot_the_practice_does_not_offer(db_session):
    practice = add_practice(db_session)
    call = add_call(db_session, practice)
    booked = book_appointment(db_session, _booking(practice, open_slot(1)), call_id=call.id)

    with pytest.raises(ToolError, match="isn't an appointment time"):
        reschedule_appointment(db_session, RescheduleAppointmentInput(
            appointment_id=booked.appointment_id, new_slot=datetime(2030, 1, 13, 15, 0),  # Sunday
        ))


# ── Idempotent per call and slot (barge-in retry) ───────────────────────────
def test_booking_the_same_slot_twice_on_one_call_returns_the_same_appointment(db_session):
    practice = add_practice(db_session)
    call = add_call(db_session, practice)

    first = book_appointment(db_session, _booking(practice, open_slot(1)), call_id=call.id)
    second = book_appointment(db_session, _booking(practice, open_slot(1)), call_id=call.id)

    assert second.appointment_id == first.appointment_id
    assert db_session.query(Appointment).count() == 1


def test_a_different_call_still_cannot_take_a_booked_slot(db_session):
    practice = add_practice(db_session)
    book_appointment(db_session, _booking(practice, open_slot(1)), call_id=add_call(db_session, practice).id)

    with pytest.raises(ToolError, match="already booked"):
        book_appointment(db_session, _booking(practice, open_slot(1)), call_id=add_call(db_session, practice).id)


# ── Scheduling through the gateway (FHIR in production, the fake here) ───────
def test_check_availability_offers_at_most_three_spread_across_days(db_session):
    practice = add_practice(db_session)
    monday = next_weekday(0)

    result = check_availability(db_session, CheckAvailabilityInput(
        practice_id=practice.id, date_range=DateRange(start_date=monday, end_date=monday + timedelta(days=4)),
    ))

    local = [s.start.astimezone(ZoneInfo("America/New_York")) for s in result.slots]
    assert len(local) == 3
    assert len({d.date() for d in local}) == 3          # three different days...
    assert all((d.hour, d.minute) == (9, 0) for d in local)  # ...each at its earliest time
    assert result.slots[0].practitioner == "Dr. Test"


def test_booking_writes_the_scheduling_system_and_a_shadow_row(db_session, schedule):
    practice = add_practice(db_session)
    call = add_call(db_session, practice)

    result = book_appointment(db_session, _booking(practice, open_slot(1)), call_id=call.id)

    booked = schedule.appointments[result.fhir_appointment_id]
    assert booked["status"] == "booked" and booked["call_id"] == call.id
    assert booked["patient"].phone == "+18135550142"  # E.164: FHIR phone search is exact
    shadow = db_session.get(Appointment, result.appointment_id)
    assert shadow.fhir_appointment_id == result.fhir_appointment_id


def test_a_slot_taken_between_check_and_write_gets_the_next_open_time(db_session, schedule, monkeypatch):
    # The race, deterministically: someone else books the slot after we read it.
    import app.agent.tools as tools_module

    practice = add_practice(db_session)
    call = add_call(db_session, practice)
    real_check = tools_module.check_booking

    def check_then_lose_the_race(*args, **kwargs):
        check = real_check(*args, **kwargs)
        schedule.mark_busy(practice, check.slot)
        return check
    monkeypatch.setattr(tools_module, "check_booking", check_then_lose_the_race)

    with pytest.raises(ToolError) as raised:
        book_appointment(db_session, _booking(practice, open_slot(1)), call_id=call.id)

    message = str(raised.value)
    assert "was just taken by someone else, so it wasn't booked" in message
    assert "The next open time is 10:30 AM" in message  # 10:00 taken -> 10:30
    assert db_session.query(Appointment).count() == 0
    # The caller's details are kept even though the booking failed: a callback is possible.
    assert db_session.query(Lead).one().callback_number == "+18135550142"


def test_an_already_booked_time_says_so_and_offers_the_next(db_session, schedule):
    practice = add_practice(db_session)
    call = add_call(db_session, practice)
    schedule.mark_busy(practice, open_slot(1))

    with pytest.raises(ToolError, match="is already booked. The next open time is 10:30 AM"):
        book_appointment(db_session, _booking(practice, open_slot(1)), call_id=call.id)


def test_cancel_updates_both_the_scheduling_system_and_postgres(db_session, schedule):
    practice = add_practice(db_session)
    call = add_call(db_session, practice)
    booked = book_appointment(db_session, _booking(practice, open_slot(1)), call_id=call.id)

    result = cancel_appointment(db_session, booked.appointment_id, practice_id=practice.id)

    assert result.status == AppointmentStatus.cancelled
    assert schedule.appointments[booked.fhir_appointment_id]["status"] == "cancelled"
    # The slot is free again: it can be booked by the next caller.
    again = book_appointment(db_session, _booking(practice, open_slot(1)), call_id=add_call(db_session, practice).id)
    assert again.fhir_appointment_id != booked.fhir_appointment_id


def test_reschedule_moves_the_booking_in_the_scheduling_system(db_session, schedule):
    practice = add_practice(db_session)
    call = add_call(db_session, practice)
    booked = book_appointment(db_session, _booking(practice, open_slot(1)), call_id=call.id)

    moved = reschedule_appointment(db_session, RescheduleAppointmentInput(
        appointment_id=booked.appointment_id, new_slot=open_slot(2),
    ), practice_id=practice.id)

    assert schedule.appointments[booked.fhir_appointment_id]["status"] == "cancelled"
    assert schedule.appointments[moved.fhir_appointment_id]["status"] == "booked"
    assert db_session.get(Appointment, booked.appointment_id).status == AppointmentStatus.rescheduled
