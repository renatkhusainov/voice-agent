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

from app.agent.schema import schema_from_model
from app.agent.spoken import spoken_time
from app.models.models import Appointment, AppointmentStatus, Call, Intent, Lead, Practice

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
    "localize_slot",
    "RescheduleAppointmentInput",
    "RescheduleAppointmentResult",
    "reschedule_appointment",
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


# ── Slot generation ──────────────────────────────────────────────────────────
# Appointments have no explicit duration in the schema, so a slot is
# considered taken only if its exact start time matches an existing
# appointment's, not by any overlap window. A real scheduling system would
# want appointment durations; this is a documented simplification, not an
# oversight.
SLOT_MINUTES = 30

# Practice.business_hours is an untyped JSON column with no shape enforced
# anywhere else in the codebase. This module is what defines one:
# {"mon": ["09:00", "17:00"], ..., "sat": None, "sun": None} — a day that's
# missing or maps to null/None is closed. Used as the fallback when a
# practice hasn't set business_hours at all.
DEFAULT_BUSINESS_HOURS: dict[str, list[str] | None] = {
    "mon": ["09:00", "17:00"],
    "tue": ["09:00", "17:00"],
    "wed": ["09:00", "17:00"],
    "thu": ["09:00", "17:00"],
    "fri": ["09:00", "17:00"],
    "sat": None,
    "sun": None,
}


def _day_hours(business_hours: dict | None, day: date) -> tuple[dt_time, dt_time] | None:
    hours = business_hours or DEFAULT_BUSINESS_HOURS
    window = hours.get(day.strftime("%a").lower())
    if not window:
        return None
    open_s, close_s = window
    return dt_time.fromisoformat(open_s), dt_time.fromisoformat(close_s)


def _practice_tz(practice: Practice) -> ZoneInfo:
    try:
        return ZoneInfo(practice.timezone)
    except ZoneInfoNotFoundError:
        raise ToolError(f"Practice {practice.id} has an unrecognized timezone: {practice.timezone!r}")


def _candidate_slots(practice: Practice, date_range: "DateRange") -> list[datetime]:
    """Every slot start, in UTC, that falls inside `practice`'s business hours
    (interpreted in the practice's own timezone — a 9am slot means 9am
    locally, not 9am UTC) somewhere in date_range, and hasn't already passed.
    """
    tz = _practice_tz(practice)

    now = datetime.now(timezone.utc)
    step = timedelta(minutes=SLOT_MINUTES)
    slots = []
    day = date_range.start_date
    while day <= date_range.end_date:
        window = _day_hours(practice.business_hours, day)
        if window:
            open_t, close_t = window
            cursor = datetime.combine(day, open_t, tzinfo=tz)
            close_dt = datetime.combine(day, close_t, tzinfo=tz)
            while cursor + step <= close_dt:
                slot_utc = cursor.astimezone(timezone.utc)
                if slot_utc > now:
                    slots.append(slot_utc)
                cursor += step
        day += timedelta(days=1)
    return slots


def _as_utc(value: datetime) -> datetime:
    """Every DateTime(timezone=True) column in this app only ever stores UTC
    (see app/models/models.py's utcnow()), but SQLite — used in tests, never
    in production — has no native timestamptz type and hands values back
    naive regardless of what was written. Treat a naive value as UTC rather
    than as ambiguous, so a value's tz-awareness never depends on which
    database it happened to round-trip through."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _booked_slot_starts(
    db: Session, practice_id: int, window_start: datetime, window_end: datetime
) -> set[datetime]:
    """Start times already booked (pending or confirmed) for this practice in
    [window_start, window_end). Appointment has no direct practice_id — it
    joins through Lead -> Call to reach one."""
    stmt = (
        select(Appointment.requested_slot)
        .join(Lead, Appointment.lead_id == Lead.id)
        .join(Call, Lead.call_id == Call.id)
        .where(
            Call.practice_id == practice_id,
            Appointment.status.in_((AppointmentStatus.pending, AppointmentStatus.confirmed)),
            Appointment.requested_slot >= window_start,
            Appointment.requested_slot < window_end,
        )
    )
    return {_as_utc(ts) for ts in db.scalars(stmt).all()}


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


class AvailableSlot(BaseModel):
    start: datetime
    end: datetime
    # What to say, as opposed to `start`, which is what to pass back to
    # book_appointment. Pre-formatted in the practice's timezone — see
    # app/agent/spoken.py for why this isn't left to the model.
    spoken: str


class CheckAvailabilityResult(BaseModel):
    practice_id: int
    slots: list[AvailableSlot]


def check_availability(db: Session, payload: CheckAvailabilityInput) -> CheckAvailabilityResult:
    """Open appointment slots for a practice over a range of dates, in
    SLOT_MINUTES increments, honoring the practice's business hours and
    timezone, excluding slots that are already booked or already past."""
    practice = db.get(Practice, payload.practice_id)
    if practice is None:
        raise ToolError(f"No practice with id {payload.practice_id}")

    candidates = _candidate_slots(practice, payload.date_range)
    if not candidates:
        return CheckAvailabilityResult(practice_id=practice.id, slots=[])

    window_start = candidates[0]
    window_end = candidates[-1] + timedelta(minutes=SLOT_MINUTES)
    booked = _booked_slot_starts(db, practice.id, window_start, window_end)

    free = [s for s in candidates if s not in booked]
    tz = _practice_tz(practice)
    return CheckAvailabilityResult(
        practice_id=practice.id,
        slots=[
            AvailableSlot(start=s, end=s + timedelta(minutes=SLOT_MINUTES), spoken=spoken_time(s, tz))
            for s in free
        ],
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


def book_appointment(db: Session, payload: BookAppointmentInput, *, call_id: int) -> BookAppointmentResult:
    """Book a new appointment once the caller has agreed to a specific slot.
    `call_id` identifies which call this is for — it's session plumbing the
    pipeline supplies, not something the model states, so it's a keyword
    argument here rather than a field on BookAppointmentInput.
    """
    call = db.get(Call, call_id)
    if call is None:
        raise ToolError(f"No call with id {call_id}")
    if call.practice_id != payload.practice_id:
        # Cross-checked against the call's actual practice rather than trusted
        # outright — practice_id is model input, so it's whatever the LLM sent.
        raise ToolError("practice_id does not match this call's practice")

    tz = _practice_tz(call.practice)
    slot = localize_slot(payload.requested_slot, tz)
    booked = _booked_slot_starts(db, payload.practice_id, slot, slot + timedelta(minutes=SLOT_MINUTES))
    if slot in booked:
        raise ToolError(f"{spoken_time(slot, tz)} is already booked")

    # No first-class `service` column exists on Appointment/Lead — that would
    # be the right long-term home for it, but adding one is a schema change
    # out of scope here. Folded into Lead.notes for now, same as
    # reschedule_appointment already does with its `reason`.
    notes = f"Service: {payload.service}" + (f"\n{payload.notes}" if payload.notes else "")
    lead = _get_or_create_lead(
        db, call_id,
        name=payload.caller_name,
        callback_number=payload.callback_number,
        intent=Intent.appointment,
        notes=notes,
    )
    appointment = Appointment(lead_id=lead.id, requested_slot=slot, status=AppointmentStatus.pending)
    db.add(appointment)
    db.commit()
    db.refresh(appointment)
    requested_slot = _as_utc(appointment.requested_slot)
    return BookAppointmentResult(
        appointment_id=appointment.id,
        lead_id=lead.id,
        status=appointment.status,
        requested_slot=requested_slot,
        spoken_time=spoken_time(requested_slot, tz),
    )


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


def reschedule_appointment(db: Session, payload: RescheduleAppointmentInput) -> RescheduleAppointmentResult:
    """Move an existing appointment to a new slot. Implemented as closing out
    the old Appointment row (status=rescheduled) and opening a new one
    (status=pending) rather than mutating the old row's slot in place, so the
    rescheduling itself stays in the history instead of being overwritten.
    """
    appointment = db.get(Appointment, payload.appointment_id)
    if appointment is None:
        raise ToolError(f"No appointment with id {payload.appointment_id}")
    if appointment.status in (AppointmentStatus.cancelled, AppointmentStatus.rescheduled):
        raise ToolError(f"Appointment {appointment.id} is {appointment.status.value} and can't be rescheduled")

    practice = appointment.lead.call.practice
    tz = _practice_tz(practice)
    new_slot = localize_slot(payload.new_slot, tz)
    booked = _booked_slot_starts(db, practice.id, new_slot, new_slot + timedelta(minutes=SLOT_MINUTES))
    if new_slot in booked:
        raise ToolError(f"{spoken_time(new_slot, tz)} is already booked")

    appointment.status = AppointmentStatus.rescheduled
    appointment.confirmation = None
    if payload.reason:
        lead = appointment.lead
        note = f"Rescheduled: {payload.reason}"
        lead.notes = f"{lead.notes}\n{note}" if lead.notes else note

    new_appointment = Appointment(
        lead_id=appointment.lead_id, requested_slot=new_slot, status=AppointmentStatus.pending
    )
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
    )


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
        "reschedule_appointment": ToolHandler(RescheduleAppointmentInput, reschedule_appointment),
        "escalate_to_human": ToolHandler(EscalateToHumanInput, partial(escalate_to_human, call_id=call_id)),
        "answer_faq": ToolHandler(AnswerFaqInput, lambda db, payload: answer_faq(payload)),
    }
