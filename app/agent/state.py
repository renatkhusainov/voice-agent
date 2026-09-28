"""DialogState: the state machine's own record of a conversation —
authoritative over whatever the model believes, not just a summary of it.

One rule drives everything here: **the LLM proposes, the state machine
decides.** The model never writes `DialogState` directly by asserting
something in conversation; only a tool call, validated and executed by our
own code, does. Every turn, `app/agent/prompts.py` renders the current state
into the system prompt ("Collected so far: name ✓ (Dana Lee), phone ✗") so
the model can see exactly what's missing — but seeing it and being trusted to
have collected it are different things; the checklist is read-only to the
model, not something it can check off by claiming to.

`DialogState` is persisted in Redis (app/agent/store.py), keyed
`call:<call_id>` on a live call (app/agent/live.py), or by session_id in
the text-mode harness (app/agent/session.py) — a process restart does not
lose it, unlike the raw conversation history in session.py's in-memory
`_SESSIONS`, which is a deliberate scope line, not an oversight: see that
module's docstring.
"""

import re
from datetime import datetime
from enum import Enum
from functools import partial
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent.prompts import PROMPT_VERSION
from app.agent.spoken import spoken_digits, spoken_time
from app.agent.tools import (
    BookAppointmentInput, EscalateToHumanInput, ToolHandler, book_appointment, build_dispatch,
    check_availability, escalate_to_human, localize_slot,
)
from app.models.models import Call

__all__ = [
    "DialogIntent",
    "BookingSlots",
    "DialogState",
    "BookingProposalResult",
    "build_gated_dispatch",
]


class DialogIntent(str, Enum):
    unknown = "unknown"
    check_availability = "check_availability"
    book_appointment = "book_appointment"
    reschedule_appointment = "reschedule_appointment"
    faq = "faq"
    escalate = "escalate"


# The literal four things the confirmation gate reads back (item 4): name,
# phone, service, time. practice_id is tracked too (check_availability fills
# it in) but isn't shown on the caller-facing checklist — it's plumbing the
# system prompt already pins, not something a caller states.
_CHECKLIST: tuple[tuple[str, str], ...] = (
    ("caller_name", "name"),
    ("callback_number", "phone"),
    ("service", "service"),
    ("requested_slot", "time"),
)


class BookingSlots(BaseModel):
    """What's been collected toward a booking so far. Every field starts
    unknown (None) and is filled in only by a tool call — never by the
    model simply stating "the caller's name is Dana" in its own reply."""

    practice_id: int | None = None
    caller_name: str | None = None
    callback_number: str | None = None
    service: str | None = None
    requested_slot: datetime | None = None
    notes: str | None = None

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields()

    def missing_fields(self) -> list[str]:
        return [label for field, label in _CHECKLIST if getattr(self, field) is None]


def _last_four(phone: str) -> str:
    """Digits only, last four — or fewer if the number is shorter than that.
    Matches the system prompt's existing rule (app/agent/prompts.py) never to
    read a sensitive number back to a caller in full."""
    digits = re.sub(r"\D", "", phone)
    return digits[-4:] if digits else ""


def _format_slot_value(field: str, value) -> str:
    if field == "callback_number":
        return f"ending in {_last_four(value)}"
    if field == "requested_slot":
        return value.isoformat()
    return str(value)


class DialogState(BaseModel):
    """One conversation's state-machine record. See module docstring for the
    one rule this exists to enforce."""

    intent: DialogIntent = DialogIntent.unknown
    slots: BookingSlots = Field(default_factory=BookingSlots)
    pending_confirmation: bool = False
    confirmed: bool = False
    turn_count: int = 0
    prompt_version: str = PROMPT_VERSION

    # Internal bookkeeping for the confirmation gate: the turn_count value at
    # the moment a booking was proposed. A later confirming call is only
    # honored once turn_count has advanced past this — i.e. at least one real
    # inbound message happened in between. Not part of the public checklist.
    proposed_at_turn: int | None = None

    def should_show_progress(self) -> bool:
        """Whether the system prompt should render a progress section at
        all — omitted before the caller has said anything booking-related,
        so turn one doesn't open with a wall of ✗."""
        return self.intent != DialogIntent.unknown or any(
            getattr(self.slots, field) is not None for field, _ in _CHECKLIST
        )

    def summary_for_prompt(self) -> str:
        """Rendered into the system prompt — the model's own view of what's
        collected. Phone stays masked here too, same as `read_back_text`: the
        model only needs to know the field is filled to decide what to ask
        next, and the actual value it passes to book_appointment comes from
        the caller's own words in the conversation, not from re-reading this
        summary — so there's no reason for the raw digits to appear here at
        all. One fewer place a sensitive number could leak from."""
        parts = [
            f"{label} ✓ ({_format_slot_value(field, getattr(self.slots, field))})"
            if getattr(self.slots, field) is not None
            else f"{label} ✗"
            for field, label in _CHECKLIST
        ]
        return "Collected so far: " + ", ".join(parts) + "."

    def read_back_text(self, tz: ZoneInfo | None = None) -> str:
        """What the model is told to say to the caller, verbatim, before
        booking — so it's written to be *heard*: the time in the practice's
        timezone as "9:30 AM Tuesday the 15th", and the phone masked to its
        last four digits, spaced so TTS reads them one at a time (see
        app/agent/spoken.py). Without `tz` the time falls back to ISO/UTC,
        which is correct but not something to say out loud."""
        s = self.slots
        phone = f"ending in {spoken_digits(_last_four(s.callback_number))}" if s.callback_number else "(missing)"
        if s.requested_slot is None:
            time_str = "(missing)"
        else:
            time_str = spoken_time(s.requested_slot, tz) if tz else s.requested_slot.isoformat()
        return (
            f"{s.caller_name or '(missing name)'}, phone number {phone}, "
            f"for a {s.service or '(missing service)'} at {time_str}"
        )


class BookingProposalResult(BaseModel):
    """What book_appointment's tool_result carries back to the model when the
    state machine hasn't yet seen a confirmed "yes" — never a booking
    confirmation, however the model might be tempted to phrase it."""

    status: Literal["pending_confirmation"] = "pending_confirmation"
    read_back: str
    instruction: str = (
        "Not booked yet. Read this back to the caller verbatim and wait for an explicit yes "
        "before calling book_appointment again with the same details."
    )


def _call_tz(db: Session, call_id: int) -> ZoneInfo | None:
    """The timezone of the practice this call belongs to — taken from the
    Call row, not from the model's practice_id. None if either is missing or
    the zone is unknown: the read-back then falls back to ISO rather than
    failing a proposal over formatting. (book_appointment itself still
    rejects a bad call or practice when the booking actually runs.)"""
    call = db.get(Call, call_id)
    if call is None or call.practice is None:
        return None
    try:
        return ZoneInfo(call.practice.timezone)
    except ZoneInfoNotFoundError:
        return None


def _slots_from_payload(payload: BookAppointmentInput) -> BookingSlots:
    return BookingSlots(
        practice_id=payload.practice_id, caller_name=payload.caller_name,
        callback_number=payload.callback_number, service=payload.service,
        requested_slot=payload.requested_slot, notes=payload.notes,
    )


def gated_book_appointment(
    db: Session, payload: BookAppointmentInput, *, call_id: int, state: DialogState,
) -> BaseModel:
    """The confirmation gate. book_appointment's real, DB-writing code
    (app/agent/tools.py) never runs on the *first* call with a given set of
    details — only once the model calls this tool again, on a later turn,
    with the identical details. That's the proof a read-back actually
    happened and the caller had a turn to respond: not the model's word for
    it, a fact the state machine itself can check (`turn_count >
    proposed_at_turn`). A single tool-use batch calling this twice in the
    same turn — no intervening caller message — still can't confirm anything,
    because turn_count only advances in app/agent/session.py's take_turn,
    once per real inbound message.
    """
    # Resolve the slot to UTC first (no offset = practice-local, see
    # tools.localize_slot), so the same time compares equal however the
    # model wrote it, and the read-back says it in local time.
    tz = _call_tz(db, call_id)
    if tz is not None:
        payload = payload.model_copy(update={"requested_slot": localize_slot(payload.requested_slot, tz)})
    proposed = _slots_from_payload(payload)

    # Exact match on purpose: this is the "decides," not the "proposes." The
    # trade-off is real, not hidden — if the model restates a field even
    # slightly differently on the confirming call (a rephrased service name,
    # say), this reads as a *new* proposal and re-triggers the read-back
    # rather than booking. Given this project's own live-testing found the
    # model inconsistent about restating things identically (see the
    # "next Tuesday" date-math findings), that's a real source of possible
    # friction — accepted here because the alternative (fuzzy matching) is
    # exactly the "prompt hope" this gate exists to not be.
    already_confirmed = (
        state.pending_confirmation
        and state.proposed_at_turn is not None
        and state.turn_count > state.proposed_at_turn
        and state.slots == proposed
    )

    if not already_confirmed:
        state.intent = DialogIntent.book_appointment
        state.slots = proposed
        state.pending_confirmation = True
        state.confirmed = False
        state.proposed_at_turn = state.turn_count
        return BookingProposalResult(read_back=state.read_back_text(tz))

    result = book_appointment(db, payload, call_id=call_id)
    state.confirmed = True
    state.pending_confirmation = False
    return result


def _stateful_check_availability(db, payload, *, state: DialogState):
    result = check_availability(db, payload)
    if state.intent == DialogIntent.unknown:
        state.intent = DialogIntent.check_availability
    if state.slots.practice_id is None:
        state.slots.practice_id = payload.practice_id
    return result


def _stateful_escalate(db, payload, *, call_id: int, state: DialogState):
    result = escalate_to_human(db, payload, call_id=call_id)
    # Set only after the tool succeeded: a failed escalation isn't one.
    state.intent = DialogIntent.escalate
    state.pending_confirmation = False
    return result


def build_gated_dispatch(call_id: int, state: DialogState) -> dict[str, ToolHandler]:
    """Same five tools as tools.build_dispatch, except book_appointment goes
    through the confirmation gate above, and check_availability and
    escalate_to_human write `intent` (and practice_id) into state as a side
    effect ("tools write state"). An escalation also drops any pending
    booking proposal, so nothing can be confirmed after the call has been
    handed off. reschedule_appointment and answer_faq are unchanged; gating
    reschedule is a real next step, not done here — see
    docs/notes/conversation-state-machine.md's open questions.
    """
    dispatch = build_dispatch(call_id)
    dispatch["book_appointment"] = ToolHandler(
        BookAppointmentInput, partial(gated_book_appointment, call_id=call_id, state=state)
    )
    dispatch["check_availability"] = ToolHandler(
        dispatch["check_availability"].input_model, partial(_stateful_check_availability, state=state)
    )
    dispatch["escalate_to_human"] = ToolHandler(
        EscalateToHumanInput, partial(_stateful_escalate, call_id=call_id, state=state)
    )
    return dispatch
