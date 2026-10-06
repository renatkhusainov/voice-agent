"""Where appointments actually live, behind one small interface.

app/agent/tools.py decides *what* may be booked (the confirmation gate,
idempotency, tenant scope, wording for the caller). A ScheduleGateway is
*where* it gets booked: free Slots, and atomic book / reschedule / cancel.
Production uses FhirScheduleGateway (app/scheduling/fhir.py): the FHIR
server is the system of record for scheduling (docs/fhir-mapping.md). Tests
above that layer use an in-memory fake (tests/scheduling_fake.py), so the
agent's ~300 offline tests don't need a FHIR server. The FHIR gateway itself
is tested against a real HAPI (tests/test_fhir_scheduling.py).
"""

import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

__all__ = [
    "SLOT_MINUTES",
    "DEFAULT_BUSINESS_HOURS",
    "PatientInfo",
    "ScheduleGateway",
    "SchedulingError",
    "SchedulingUnavailable",
    "SlotRef",
    "SlotTaken",
    "gateway",
    "normalize_phone",
    "service_code",
    "set_gateway",
    "slot_starts",
]

# A slot is 30 minutes everywhere: the FHIR seed, the fake, the tools.
SLOT_MINUTES = 30

# Practice.business_hours shape: {"mon": ["09:00", "17:00"], ..., "sat": None}.
# A day that's missing or null is closed. Used when a practice set none.
DEFAULT_BUSINESS_HOURS: dict[str, list[str] | None] = {
    "mon": ["09:00", "17:00"], "tue": ["09:00", "17:00"], "wed": ["09:00", "17:00"],
    "thu": ["09:00", "17:00"], "fri": ["09:00", "17:00"], "sat": None, "sun": None,
}
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def slot_starts(tz_name: str, business_hours: dict | None, first_day: date, days: int) -> list[datetime]:
    """Every SLOT_MINUTES start inside business hours over `days` days from
    `first_day`, in the practice's own timezone (9:00 means 9:00 local), as
    UTC. The one definition of "a slot", used to seed FHIR and by the fake."""
    tz = ZoneInfo(tz_name)
    hours = business_hours or DEFAULT_BUSINESS_HOURS
    step = timedelta(minutes=SLOT_MINUTES)
    starts = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        window = hours.get(_DAYS[day.weekday()])
        if not window:
            continue
        cursor = datetime.combine(day, time.fromisoformat(window[0]), tzinfo=tz)
        close = datetime.combine(day, time.fromisoformat(window[1]), tzinfo=tz)
        while cursor + step <= close:
            starts.append(cursor.astimezone(timezone.utc))
            cursor += step
    return starts


def normalize_phone(number: str) -> str:
    """A US/NANP number in E.164 ("+18135550142"), the form Twilio gives as
    caller ID. FHIR phone search is an exact string match, so every number
    written to or searched in FHIR goes through this. Anything that isn't
    recognisably NANP is returned as given (digits and a leading +)."""
    digits = re.sub(r"\D", "", number)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}" if number.strip().startswith("+") and digits else number.strip()


# What a caller says -> the service codes Schedules and Slots are seeded with
# (scripts/seed_fhir.py): cleaning (hygienist), exam and filling (dentist).
_SERVICE_WORDS = {"clean": "cleaning", "hygien": "cleaning", "prophy": "cleaning",
                  "check": "exam", "exam": "exam", "consult": "exam",
                  "fill": "filling", "cavit": "filling"}


def service_code(service: str | None) -> str | None:
    """"teeth cleaning" -> "cleaning", "checkup" -> "exam"; None if unknown."""
    text = (service or "").lower()
    return next((code for word, code in _SERVICE_WORDS.items() if word in text), None)


@dataclass(frozen=True)
class SlotRef:
    """One bookable 30-minute slot as the gateway reports it. `version` is
    what a booking must still match (If-Match), or it lost a race."""

    id: str
    version: str
    start: datetime
    end: datetime
    status: str  # "free" | "busy" | ...
    practitioner: str | None = None       # "Practitioner/1027"
    practitioner_name: str | None = None  # "Dr. Mei Chen"
    location: str | None = None
    services: tuple[str, ...] = ()        # service codes this slot is for: "cleaning", "exam", ...
    resource: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class PatientInfo:
    name: str
    phone: str  # E.164 (normalize_phone)


class SchedulingError(Exception):
    """The scheduling system refused the request (not a race)."""


class SchedulingUnavailable(SchedulingError):
    """The scheduling system can't be reached. Don't book anywhere else."""


class SlotTaken(SchedulingError):
    """Someone else booked this slot between our read and our write."""


class ScheduleGateway(Protocol):
    def slots(self, practice: Any, start: datetime, end: datetime, *, free_only: bool = True) -> list[SlotRef]:
        """Slots of this practice with start in [start, end), sorted by start."""

    def book(self, practice: Any, slot: SlotRef, patient: PatientInfo, *, service: str,
             notes: str | None = None, call_id: int | None = None) -> str:
        """Atomically: create the Appointment and mark the slot busy, only if
        the slot is still at `slot.version`. Returns the appointment's id in
        the scheduling system. SlotTaken if the slot moved on."""

    def reschedule(self, practice: Any, appointment_id: str, new_slot: SlotRef, *,
                   reason: str | None = None) -> str:
        """Atomically: cancel the old appointment, free its slot, book
        `new_slot`. Returns the new appointment's id."""

    def cancel(self, practice: Any, appointment_id: str) -> None:
        """Atomically: cancel the appointment and free its slot."""


_gateway: ScheduleGateway | None = None
_lock = threading.Lock()


def gateway() -> ScheduleGateway:
    """The process's gateway: FHIR, built on first use."""
    global _gateway
    with _lock:
        if _gateway is None:
            from app.scheduling.fhir import FhirScheduleGateway
            _gateway = FhirScheduleGateway()
        return _gateway


def set_gateway(new: ScheduleGateway | None) -> None:
    """Swap the gateway (tests); None goes back to building the FHIR one."""
    global _gateway
    with _lock:
        _gateway = new
