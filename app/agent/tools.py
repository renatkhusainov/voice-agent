"""The five tools the front-desk agent can call: check_availability,
book_appointment, reschedule_appointment, escalate_to_human, and answer_faq
(a stub until module 6 builds real FAQ retrieval).

Each tool is a plain Python function plus a Pydantic input model. The model
is the only place a tool's parameters are described — its JSON schema is
generated from it via app/agent/schema.py's schema_from_model(), never
hand-written a second time. Editing a field on the model is the whole edit;
the schema the LLM sees follows automatically.

What's in a tool's Pydantic model vs. passed as a plain function argument is
a deliberate line: fields the caller has to actually decide or state (which
practice, which dates, a name, a time) are on the model, because those are
exactly what an LLM should be reasoning about and the JSON schema should
describe. Session plumbing that no caller ever "says" — the DB session, and
which call this is — is never in the schema; it's an ordinary keyword
argument the pipeline supplies when it invokes the tool, the same way a
FastAPI route takes `db: Session = Depends(get_db)` alongside a request body.
`practice_id` is the one identifier that IS on a model (CheckAvailabilityInput):
call_id genuinely can't be known by an LLM unless it's told, but "which
practice" is exactly the kind of fact a system prompt can pin and the model
echo back on every call, so it's modeled as real tool input, not plumbing.

Exposed two ways: as Pipecat `FunctionSchema`/`ToolsSchema` objects (`TOOLS`
at the bottom) for advertising to an LLM, and as a `ToolHandler` per tool
(`build_dispatch()`) for actually calling one by name — app/agent/loop.py's
tool-use loop takes a `dispatch: dict[str, ToolHandler]` built by
`build_dispatch(call_id)` and doesn't need to know anything else about these
five tools' individual signatures.

On a live call these same functions run through app/agent/live.py, which
registers them with Pipecat's AnthropicLLMService (app/services/bot.py).
"""

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from functools import partial
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, Field, model_validator
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from sqlalchemy import select
from sqlalchemy.orm import Session

from loguru import logger

from app.agent.schema import schema_from_model
from app.agent.spoken import spoken_time
from app.models.models import Appointment, AppointmentStatus, Call, Intent, Lead, Practice
from app.scheduling import (
    DEFAULT_BUSINESS_HOURS,
    SLOT_MINUTES,
    PatientInfo,
    SchedulingError,
    SchedulingUnavailable,
    SlotRef,
    SlotTaken,
    gateway,
    normalize_phone,
    service_code,
)

__all__ = [
    "ToolError",
    "ToolHandler",
    "build_dispatch",
    "DateRange",
    "AvailableSlot",
    "CheckAvailabilityInput",
    "CheckAvailabilityResult",
    "check_availability",
    "BookAppointmentInput",
    "BookAppointmentResult",
    "book_appointment",
    "business_hours",
    "BookingCheck",
    "check_booking",
    "localize_slot",
    "RescheduleAppointmentInput",
    "RescheduleAppointmentResult",
    "reschedule_appointment",
    "CancelAppointmentResult",
    "cancel_appointment",
    "ESCALATION_MESSAGE",
    "EscalateToHumanInput",
    "EscalateToHumanResult",
    "escalate_to_human",
    "AnswerFaqInput",
    "AnswerFaqResult",
    "answer_faq",
    "TOOLS",
]


class ToolError(Exception):
    """A tool couldn't do what was asked (unknown id, slot already taken,
    ...) — a normal conversational outcome, not a bug. The caller (the
    eventual pipeline adapter) is expected to catch this and let the
    assistant say something sensible, not crash the call."""


@dataclass(frozen=True)
class ToolHandler:
    """Binds one tool's name to the Pydantic model that validates its input
    and the function that runs it — everything app/agent/loop.py's tool-use
    loop needs to call a tool by name without knowing anything else about it,
    in particular nothing about which of these five take a call_id."""

    input_model: type[BaseModel]
    call: Callable[[Session, BaseModel], BaseModel]


# ── Scheduling: where slots and appointments live ─────────────────────────────
# The practice's scheduling system owns slots and appointments: FHIR, the
# system of record (docs/fhir-mapping.md), reached through
# app.scheduling.gateway(). These tools decide what may be booked and how to
# say it; the gateway books. The Postgres Appointment row is a shadow: which
# call booked which FHIR Appointment (fhir_appointment_id).
MAX_OPTIONS = 3  # check_availability offers at most this many times


def _practice_tz(practice: Practice) -> ZoneInfo:
    try:
        return ZoneInfo(practice.timezone)
    except ZoneInfoNotFoundError:
        raise ToolError(f"Practice {practice.id} has an unrecognized timezone: {practice.timezone!r}")


def business_hours(practice: Practice) -> dict[str, str]:
    """The practice's opening hours as people read them: {"mon": "09:00-17:00",
    ..., "sun": "closed"}, falling back to DEFAULT_BUSINESS_HOURS. The MCP
    hours resource serves this; the FHIR seed turns the same hours into Slots."""
    hours = practice.business_hours or DEFAULT_BUSINESS_HOURS
    out = {}
    for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        window = hours.get(day)
        out[day] = f"{window[0]}-{window[1]}" if window else "closed"
    return out


def _as_utc(value: datetime) -> datetime:
    """Every DateTime(timezone=True) column in this app only ever stores UTC
    (see app/models/models.py's utcnow()), but SQLite — used in tests, never
    in production — has no native timestamptz type and hands values back
    naive regardless of what was written. Treat a naive value as UTC rather
    than as ambiguous, so a value's tz-awareness never depends on which
    database it happened to round-trip through."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _scheduling(fn, *args, **kwargs):
    """Call the scheduling system; turn its failures into something the
    caller can be told. SlotTaken passes through: callers turn it into "that
    time was just taken, here's the next one"."""
    try:
        return fn(*args, **kwargs)
    except SlotTaken:
        raise
    except SchedulingUnavailable:
        raise ToolError(
            "The scheduling system isn't reachable right now, so nothing was booked. "
            "Offer to have the office call back."
        ) from None
    except SchedulingError as exc:
        raise ToolError(f"The scheduling system couldn't do that: {exc}") from None


def _prefer_service(slots: list[SlotRef], service: str | None) -> list[SlotRef]:
    """Slots for this service if there are any (a cleaning goes on the
    hygienist's calendar), otherwise all of them."""
    code = service_code(service)
    matching = [s for s in slots if code and code in s.services]
    return matching or slots


def _pick_options(slots: list[SlotRef], tz: ZoneInfo, n: int = MAX_OPTIONS) -> list[SlotRef]:
    """At most n distinct times, spread across days: the earliest time of each
    day first, then the next earliest. A caller hears "Monday at 9, Tuesday at
    9, Wednesday at 9", not three back-to-back half hours."""
    by_start: dict[datetime, SlotRef] = {}
    for slot in slots:
        by_start.setdefault(slot.start, slot)
    distinct = sorted(by_start.values(), key=lambda s: s.start)
    picked: list[SlotRef] = []
    days: set[date] = set()
    for slot in distinct:
        day = slot.start.astimezone(tz).date()
        if day not in days and len(picked) < n:
            picked.append(slot)
            days.add(day)
    for slot in distinct:
        if len(picked) >= n:
            break
        if slot not in picked:
            picked.append(slot)
    return sorted(picked, key=lambda s: s.start)


def _next_open(practice: Practice, after: datetime, service: str | None) -> SlotRef | None:
    try:
        upcoming = _scheduling(gateway().slots, practice, after + timedelta(minutes=1), after + timedelta(days=7))
    except ToolError:
        return None
    upcoming = _prefer_service(upcoming, service)
    return upcoming[0] if upcoming else None


def _next_open_sentence(practice: Practice, after: datetime, tz: ZoneInfo, service: str | None) -> str:
    nxt = _next_open(practice, after, service)
    if nxt is None:
        return " There's nothing else open in the following week."
    return f" The next open time is {spoken_time(nxt.start, tz)} (start {nxt.start.isoformat()})."


def _find_free_slot(practice: Practice, when: datetime, tz: ZoneInfo, service: str | None) -> SlotRef:
    """The free slot this booking would take. Checked here, not only in
    check_availability, because nothing forces the model to call
    check_availability first: "Sunday at 3 AM" must fail, not book."""
    if when <= datetime.now(timezone.utc):
        raise ToolError(f"{spoken_time(when, tz)} has already passed. Offer a time in the future.")
    at = [s for s in _scheduling(gateway().slots, practice, when, when + timedelta(minutes=SLOT_MINUTES),
                                 free_only=False) if s.start == when]
    if not at:
        raise ToolError(
            f"{spoken_time(when, tz)} isn't an appointment time this practice offers. "
            "Use check_availability to find an open slot."
        )
    free = [s for s in at if s.status == "free"]
    if not free:
        raise ToolError(f"{spoken_time(when, tz)} is already booked." + _next_open_sentence(practice, when, tz, service))
    return _prefer_service(free, service)[0]


def _get_or_create_lead(
    db: Session,
    call_id: int,
    *,
    name: str | None = None,
    callback_number: str | None = None,
    intent: Intent | None = None,
    notes: str | None = None,
) -> Lead:
    """Every call has at most one Lead (call_id is unique on leads) — reuse it
    across tool calls within the same call rather than creating a second one,
    and only overwrite a field the caller actually gave a value for."""
    if db.get(Call, call_id) is None:
        raise ToolError(f"No call with id {call_id}")

    lead = db.scalars(select(Lead).where(Lead.call_id == call_id)).first()
    if lead is None:
        lead = Lead(call_id=call_id)
        db.add(lead)

    if name is not None:
        lead.name = name
    if callback_number is not None:
        lead.callback_number = callback_number
    if intent is not None:
        lead.intent = intent
    if notes:
        lead.notes = f"{lead.notes}\n{notes}" if lead.notes else notes

    db.flush()
    return lead


# ── 1. check_availability ────────────────────────────────────────────────────
class DateRange(BaseModel):
    start_date: date = Field(description="First day to check, inclusive")
    end_date: date = Field(description="Last day to check, inclusive")

    @model_validator(mode="after")
    def _end_not_before_start(self) -> "DateRange":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class CheckAvailabilityInput(BaseModel):
    practice_id: int = Field(description="Which practice to check")
    date_range: DateRange = Field(description="The window of dates to search")
    service: str | None = Field(
        default=None,
        description="What the visit is for, if the caller said (cleaning, exam, filling): only times offered for it",
    )


class AvailableSlot(BaseModel):
    start: datetime
    end: datetime
    # What to say, as opposed to `start`, which is what to pass back to
    # book_appointment. Pre-formatted in the practice's timezone — see
    # app/agent/spoken.py for why this isn't left to the model.
    spoken: str
    practitioner: str | None = None


class CheckAvailabilityResult(BaseModel):
    practice_id: int
    slots: list[AvailableSlot]


def check_availability(db: Session, payload: CheckAvailabilityInput) -> CheckAvailabilityResult:
    """Up to MAX_OPTIONS open times for a practice over a range of dates (its
    local days), from the scheduling system's free Slots, spread across days,
    for the requested service when it's known. Past times are never offered."""
    practice = db.get(Practice, payload.practice_id)
    if practice is None:
        raise ToolError(f"No practice with id {payload.practice_id}")
    tz = _practice_tz(practice)

    start = datetime.combine(payload.date_range.start_date, dt_time(0), tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(payload.date_range.end_date + timedelta(days=1), dt_time(0), tzinfo=tz).astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if end <= now:
        return CheckAvailabilityResult(practice_id=practice.id, slots=[])

    free = [s for s in _scheduling(gateway().slots, practice, max(start, now), end) if s.start > now]
    options = _pick_options(_prefer_service(free, payload.service), tz)
    return CheckAvailabilityResult(
        practice_id=practice.id,
        slots=[AvailableSlot(start=s.start, end=s.end, spoken=spoken_time(s.start, tz), practitioner=s.practitioner_name)
               for s in options],
    )


# ── 2. book_appointment ──────────────────────────────────────────────────────
# A slot the model passes in: either an exact `start` from check_availability
# (UTC, with an offset) or the practice's own local time with no offset. Both
# are unambiguous: "no offset" is defined to mean practice-local, and
# localize_slot() turns either one into UTC before anything else sees it.
# Why accept local time at all: a live call showed the model sending
# "2026-12-15T09:30:00" for "9:30 in the morning". Making it convert to UTC
# first is exactly the date math it's unreliable at, and a wrong offset books
# the wrong hour.
_SLOT_DESCRIPTION = (
    "The appointment start time. Use the exact `start` from check_availability if you have it; "
    "otherwise the practice's LOCAL time with no UTC offset, e.g. 2026-12-15T09:30:00."
)


def localize_slot(value: datetime, tz: ZoneInfo) -> datetime:
    """A slot as UTC: a naive value is practice-local time (`tz`), an aware
    one is converted as-is."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(timezone.utc)


class BookAppointmentInput(BaseModel):
    practice_id: int = Field(description="Which practice this booking is for")
    caller_name: str = Field(min_length=1, description="The caller's name")
    callback_number: str = Field(min_length=1, description="Best number to reach the caller")
    service: str = Field(min_length=1, description="What kind of appointment — e.g. cleaning, checkup, filling")
    requested_slot: datetime = Field(description=_SLOT_DESCRIPTION)
    notes: str | None = Field(default=None, description="Anything else worth passing to the front desk")


class BookAppointmentResult(BaseModel):
    appointment_id: int
    lead_id: int
    status: AppointmentStatus
    requested_slot: AwareDatetime
    spoken_time: str
    practitioner: str | None = None
    fhir_appointment_id: str | None = None


def _own_appointment_at(db: Session, call_id: int, slot: datetime) -> Appointment | None:
    """This call's own live (pending/confirmed) appointment at `slot`, if any."""
    stmt = (
        select(Appointment)
        .join(Lead, Appointment.lead_id == Lead.id)
        .where(
            Lead.call_id == call_id,
            Appointment.status.in_((AppointmentStatus.pending, AppointmentStatus.confirmed)),
            Appointment.requested_slot >= slot,
            Appointment.requested_slot < slot + timedelta(minutes=1),
        )
    )
    return next((a for a in db.scalars(stmt).all() if _as_utc(a.requested_slot) == slot), None)


@dataclass(frozen=True)
class BookingCheck:
    """What check_booking established: the practice, the slot time in UTC, the
    practice's timezone, this call's existing appointment at that time if it
    already has one, and otherwise the free slot a booking would take."""

    practice: Practice
    slot: datetime
    tz: ZoneInfo
    existing: Appointment | None
    free_slot: SlotRef | None = None


def check_booking(db: Session, payload: BookAppointmentInput, *, call_id: int | None) -> BookingCheck:
    """Every check book_appointment makes before writing, and nothing else.
    Also run by the confirmation gate (app/agent/state.py) *before* it
    proposes, so a caller is never read back a time that would fail once
    they say yes.

    A slot this same call already booked is not an error. It comes back as
    `existing`, which makes booking idempotent. That's the case when a caller
    barges in while the confirming call is running: the booking commits, but
    the model never sees the result and tries again.
    """
    if call_id is None:
        # A read-only check with no call yet (the MCP server's confirmation
        # step): payload.practice_id was set by the caller's credential, not
        # by the model, so it's trusted here.
        practice = db.get(Practice, payload.practice_id)
        if practice is None:
            raise ToolError(f"No practice with id {payload.practice_id}")
    else:
        call = db.get(Call, call_id)
        if call is None:
            raise ToolError(f"No call with id {call_id}")
        if call.practice_id != payload.practice_id:
            # Cross-checked against the call's actual practice rather than trusted
            # outright — practice_id is model input, so it's whatever the LLM sent.
            raise ToolError("practice_id does not match this call's practice")
        practice = call.practice

    tz = _practice_tz(practice)
    slot = localize_slot(payload.requested_slot, tz)
    existing = _own_appointment_at(db, call_id, slot) if call_id is not None else None
    if existing is not None:
        return BookingCheck(practice=practice, slot=slot, tz=tz, existing=existing)
    free = _find_free_slot(practice, slot, tz, payload.service)
    return BookingCheck(practice=practice, slot=slot, tz=tz, existing=None, free_slot=free)


def _booking_result(appointment: Appointment, tz: ZoneInfo, practitioner: str | None = None) -> BookAppointmentResult:
    requested_slot = _as_utc(appointment.requested_slot)
    return BookAppointmentResult(
        appointment_id=appointment.id,
        lead_id=appointment.lead_id,
        status=appointment.status,
        requested_slot=requested_slot,
        spoken_time=spoken_time(requested_slot, tz),
        practitioner=practitioner,
        fhir_appointment_id=appointment.fhir_appointment_id,
    )


def _just_taken(practice: Practice, when: datetime, tz: ZoneInfo, service: str | None) -> ToolError:
    """The race: between our read and our write, someone else booked it. The
    scheduling system refused the write as a whole, so nothing was booked."""
    return ToolError(f"{spoken_time(when, tz)} was just taken by someone else, so it wasn't booked."
                     + _next_open_sentence(practice, when, tz, service))


def book_appointment(db: Session, payload: BookAppointmentInput, *, call_id: int) -> BookAppointmentResult:
    """Book a new appointment once the caller has agreed to a specific slot.
    `call_id` identifies which call this is for — it's session plumbing the
    pipeline supplies, not something the model states, so it's a keyword
    argument here rather than a field on BookAppointmentInput.

    The booking itself is one transaction in the scheduling system (FHIR
    Appointment created, Slot marked busy, only if the Slot is still the
    version we read). Then the Postgres shadow row records it.

    Idempotent per call and slot: booking a slot this call already holds
    returns that appointment instead of failing (see check_booking).
    """
    check = check_booking(db, payload, call_id=call_id)
    if check.existing is not None:
        return _booking_result(check.existing, check.tz)

    # No first-class `service` column exists on Appointment/Lead; it lives in
    # Lead.notes here and as Appointment.serviceType in FHIR.
    notes = f"Service: {payload.service}" + (f"\n{payload.notes}" if payload.notes else "")
    lead = _get_or_create_lead(
        db, call_id,
        name=payload.caller_name,
        callback_number=payload.callback_number,
        intent=Intent.appointment,
        notes=notes,
    )
    db.commit()  # the caller's details are kept even if the booking fails: the office can call back

    patient = PatientInfo(name=payload.caller_name, phone=normalize_phone(payload.callback_number))
    try:
        fhir_id = _scheduling(gateway().book, check.practice, check.free_slot, patient,
                              service=payload.service, notes=payload.notes, call_id=call_id)
    except SlotTaken:
        raise _just_taken(check.practice, check.slot, check.tz, payload.service) from None

    appointment = Appointment(lead_id=lead.id, requested_slot=check.slot, status=AppointmentStatus.confirmed,
                              fhir_appointment_id=fhir_id)
    db.add(appointment)
    try:
        db.commit()
    except Exception:
        # The booking exists in the system of record; only our shadow failed.
        # Say so loudly, with the id, so it can be reconciled.
        logger.error("call={} booked FHIR Appointment/{} but the Postgres shadow row failed", call_id, fhir_id)
        raise
    db.refresh(appointment)
    return _booking_result(appointment, check.tz, practitioner=check.free_slot.practitioner_name)


# ── 3. reschedule_appointment ────────────────────────────────────────────────
class RescheduleAppointmentInput(BaseModel):
    appointment_id: int = Field(description="The existing appointment to move")
    new_slot: datetime = Field(description=_SLOT_DESCRIPTION)
    reason: str | None = Field(default=None, description="Why it's being moved, if the caller said")


class RescheduleAppointmentResult(BaseModel):
    appointment_id: int
    previous_appointment_id: int
    status: AppointmentStatus
    requested_slot: AwareDatetime
    spoken_time: str
    fhir_appointment_id: str | None = None


def _scoped_appointment(db: Session, appointment_id: int, practice_id: int | None) -> Appointment:
    """`practice_id` scopes the lookup to one practice, the same isolation
    rule as /calls: an appointment belonging to another practice is reported
    as not found, not as "not yours", so its existence doesn't leak. The
    voice path binds the call's practice (build_dispatch); the MCP server
    binds the practice its API key maps to."""
    appointment = db.get(Appointment, appointment_id)
    if appointment is None or (practice_id is not None and appointment.lead.call.practice_id != practice_id):
        raise ToolError(f"No appointment with id {appointment_id}")
    if appointment.status in (AppointmentStatus.cancelled, AppointmentStatus.rescheduled):
        raise ToolError(f"Appointment {appointment.id} is {appointment.status.value} and can't be changed")
    return appointment


def _service_of(appointment: Appointment) -> str | None:
    for line in (appointment.lead.notes or "").splitlines():
        if line.startswith("Service: "):
            return line.removeprefix("Service: ")
    return None


def reschedule_appointment(
    db: Session, payload: RescheduleAppointmentInput, *, practice_id: int | None = None,
) -> RescheduleAppointmentResult:
    """Move an existing appointment to a new slot. In the scheduling system,
    one transaction: the old Appointment cancelled and its Slot freed, the new
    one booked and its Slot taken. In Postgres, the old shadow row becomes
    `rescheduled` and a new one is opened, so the history stays readable."""
    appointment = _scoped_appointment(db, payload.appointment_id, practice_id)
    practice = appointment.lead.call.practice
    tz = _practice_tz(practice)
    new_slot = localize_slot(payload.new_slot, tz)
    service = _service_of(appointment)
    free = _find_free_slot(practice, new_slot, tz, service)

    try:
        if appointment.fhir_appointment_id:
            new_fhir_id = _scheduling(gateway().reschedule, practice, appointment.fhir_appointment_id, free,
                                      reason=payload.reason)
        else:
            # Booked before scheduling moved to FHIR: nothing to cancel there.
            lead = appointment.lead
            new_fhir_id = _scheduling(
                gateway().book, practice, free,
                PatientInfo(name=lead.name or "Patient", phone=normalize_phone(lead.callback_number or "")),
                service=service or "appointment", notes=payload.reason, call_id=lead.call_id,
            )
    except SlotTaken:
        raise _just_taken(practice, new_slot, tz, service) from None

    appointment.status = AppointmentStatus.rescheduled
    appointment.confirmation = None
    if payload.reason:
        lead = appointment.lead
        note = f"Rescheduled: {payload.reason}"
        lead.notes = f"{lead.notes}\n{note}" if lead.notes else note

    new_appointment = Appointment(lead_id=appointment.lead_id, requested_slot=new_slot,
                                  status=AppointmentStatus.confirmed, fhir_appointment_id=new_fhir_id)
    db.add(new_appointment)
    db.commit()
    db.refresh(new_appointment)
    requested_slot = _as_utc(new_appointment.requested_slot)
    return RescheduleAppointmentResult(
        appointment_id=new_appointment.id,
        previous_appointment_id=appointment.id,
        status=new_appointment.status,
        requested_slot=requested_slot,
        spoken_time=spoken_time(requested_slot, tz),
        fhir_appointment_id=new_fhir_id,
    )


class CancelAppointmentResult(BaseModel):
    appointment_id: int
    status: AppointmentStatus


def cancel_appointment(db: Session, appointment_id: int, *, practice_id: int | None = None) -> CancelAppointmentResult:
    """Cancel in both places: the FHIR Appointment cancelled and its Slot
    freed (one transaction), then the Postgres shadow row. Not an agent tool
    yet: cancelling needs its own confirmation step first."""
    appointment = _scoped_appointment(db, appointment_id, practice_id)
    if appointment.fhir_appointment_id:
        try:
            _scheduling(gateway().cancel, appointment.lead.call.practice, appointment.fhir_appointment_id)
        except SlotTaken:
            raise ToolError("That appointment was changed at the same moment; check it and try again.") from None
    appointment.status = AppointmentStatus.cancelled
    db.commit()
    return CancelAppointmentResult(appointment_id=appointment.id, status=appointment.status)


# ── 4. escalate_to_human ─────────────────────────────────────────────────────
class EscalateToHumanInput(BaseModel):
    reason: str = Field(min_length=1, description="Why this needs a human — the caller's situation, briefly")


class EscalateToHumanResult(BaseModel):
    lead_id: int
    message_for_caller: str


# The warm handoff line. Deliberately contains no clinical content at all —
# no "that sounds serious," no "try a cold compress" — only that a person
# will follow up, plus the one safety line any front desk gives: an
# emergency goes to 911, not a callback queue. On a live call this is spoken
# verbatim by app/agent/live.py (not paraphrased by the model), and then the
# call ends.
ESCALATION_MESSAGE = (
    "I'm sorry you're dealing with that. I've passed this to our team and someone "
    "from the office will call you back as soon as possible. If this is a medical "
    "emergency, please hang up and call 911. Take care."
)


def escalate_to_human(db: Session, payload: EscalateToHumanInput, *, call_id: int) -> EscalateToHumanResult:
    """Hand the call off to office staff — anything clinical, or anything the
    assistant can't handle itself. Records the reason on the call's Lead
    (intent=callback) and returns the handoff line to say (ESCALATION_MESSAGE).
    """
    lead = _get_or_create_lead(db, call_id, intent=Intent.callback, notes=payload.reason)
    db.commit()
    return EscalateToHumanResult(lead_id=lead.id, message_for_caller=ESCALATION_MESSAGE)


# ── 5. answer_faq (stub until module 6) ──────────────────────────────────────
class AnswerFaqInput(BaseModel):
    question: str = Field(min_length=1, description="The caller's general question about the practice")


class AnswerFaqResult(BaseModel):
    question: str
    answered: bool
    answer: str


def answer_faq(payload: AnswerFaqInput) -> AnswerFaqResult:
    """Stub until module 6 builds real FAQ retrieval (a knowledge-base lookup
    over the practice's actual policies). No DB access, no guessing: it
    always defers, matching the system prompt's own rule in
    app/services/bot.py never to invent a practice-specific fact.
    """
    return AnswerFaqResult(
        question=payload.question,
        answered=False,
        answer="I don't have that answered yet — I'll have the office call you back.",
    )


# ── Schemas, generated from the models above — never re-typed by hand ───────
def _function_schema(name: str, description: str, model: type[BaseModel]) -> FunctionSchema:
    properties, required = schema_from_model(model)
    return FunctionSchema(name=name, description=description, properties=properties, required=required)


CHECK_AVAILABILITY_SCHEMA = _function_schema(
    "check_availability",
    "Look up open appointment slots for a practice over a range of dates.",
    CheckAvailabilityInput,
)
BOOK_APPOINTMENT_SCHEMA = _function_schema(
    "book_appointment",
    "Book a new appointment once the caller has agreed to a specific date and time.",
    BookAppointmentInput,
)
RESCHEDULE_APPOINTMENT_SCHEMA = _function_schema(
    "reschedule_appointment",
    "Move an existing appointment to a new date and time.",
    RescheduleAppointmentInput,
)
ESCALATE_TO_HUMAN_SCHEMA = _function_schema(
    "escalate_to_human",
    "Hand the call off to office staff — for anything clinical, or anything the assistant "
    "can't safely or confidently handle itself.",
    EscalateToHumanInput,
)
ANSWER_FAQ_SCHEMA = _function_schema(
    "answer_faq",
    "Answer a general question about the practice, such as hours, insurance, or policies. "
    "Stub until module 6 — currently always defers to a human.",
    AnswerFaqInput,
)

TOOLS = ToolsSchema(standard_tools=[
    CHECK_AVAILABILITY_SCHEMA,
    BOOK_APPOINTMENT_SCHEMA,
    RESCHEDULE_APPOINTMENT_SCHEMA,
    ESCALATE_TO_HUMAN_SCHEMA,
    ANSWER_FAQ_SCHEMA,
])


def _reschedule_on_call(db: Session, payload: RescheduleAppointmentInput, *, call_id: int):
    """Reschedule, scoped to the practice this call belongs to."""
    call = db.get(Call, call_id)
    if call is None:
        raise ToolError(f"No call with id {call_id}")
    return reschedule_appointment(db, payload, practice_id=call.practice_id)


def build_dispatch(call_id: int) -> dict[str, ToolHandler]:
    """The dispatch table app/agent/loop.py's run_agent_loop needs: every
    tool bound to one specific call. book_appointment and escalate_to_human
    take call_id as a keyword argument (see their own docstrings for why
    it's not on their input models); binding it here via partial() is what
    keeps the loop itself generic — it only ever calls `handler.call(db, payload)`.
    answer_faq takes no db at all, so it's wrapped to accept and ignore one,
    matching every other handler's (db, payload) -> Result shape.
    """
    return {
        "check_availability": ToolHandler(CheckAvailabilityInput, check_availability),
        "book_appointment": ToolHandler(BookAppointmentInput, partial(book_appointment, call_id=call_id)),
        "reschedule_appointment": ToolHandler(
            RescheduleAppointmentInput, partial(_reschedule_on_call, call_id=call_id)
        ),
        "escalate_to_human": ToolHandler(EscalateToHumanInput, partial(escalate_to_human, call_id=call_id)),
        "answer_faq": ToolHandler(AnswerFaqInput, lambda db, payload: answer_faq(payload)),
    }
