"""Scheduling through a real FHIR server: app/scheduling/fhir.py and the
tools on top of it, against HAPI (`docker compose up -d hapi`).

Why a real server and not recorded HTTP (respx): the behaviours that matter
here are the server's. A transaction is all-or-nothing, a stale If-Match is
refused with 409, and two concurrent bookings of one slot can't both win.
A recording replays the responses we already expect; it can't show a race
or a rollback. These tests skip when HAPI isn't reachable; in CI, HAPI runs
as a service container (the same image as docker-compose.yaml).

Each session seeds its own practice (id 990001, two days in 2030, so nothing
is in the past) and resets its slots to free first: reruns stay independent.
"""

import threading
from datetime import date, datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.tools import (
    BookAppointmentInput,
    RescheduleAppointmentInput,
    ToolError,
    book_appointment,
    cancel_appointment,
    check_availability,
    CheckAvailabilityInput,
    DateRange,
    reschedule_appointment,
)
from app.fhir.client import FhirClient
from app.models.models import Appointment, AppointmentStatus, Base, Practice
from app.scheduling import set_gateway
from app.scheduling.fhir import SID, FhirScheduleGateway
from app.services.calls import start_call
from scripts.seed_fhir import PracticeSeed, build_seed_bundle, seed

FHIR_URL = "http://localhost:8090/fhir"
PRACTICE_ID = 990001
MONDAY = date(2030, 1, 7)
TZ = "America/New_York"


def _hapi_up() -> bool:
    try:
        return httpx.get(f"{FHIR_URL}/metadata", timeout=2).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _hapi_up(), reason="HAPI not running: docker compose up -d hapi")


def at(hour: int, minute: int = 0, day: int = 7) -> datetime:
    """Local New York time on 2030-01-0<day> (EST, UTC-5), as UTC."""
    return datetime(2030, 1, day, hour + 5, minute, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def seeded():
    """Seed the test practice (idempotent) and put every one of its slots
    back to free, so each test session starts from the same calendar."""
    with FhirClient(FHIR_URL) as fhir:
        seed(fhir, build_seed_bundle(PracticeSeed(PRACTICE_ID, "FHIR Test Dental", "+15550990001", TZ, None),
                                     MONDAY, days=2))
        [location] = fhir.search("Location", {"identifier": f"{SID}/location|{PRACTICE_ID}"})
        schedules = ",".join(f"Schedule/{s['id']}" for s in fhir.search("Schedule", {"actor": f"Location/{location['id']}"}))
        for slot in fhir.search("Slot", [("schedule", schedules), ("status", "busy"), ("_count", "200")]):
            for appt in fhir.search("Appointment", {"slot": f"Slot/{slot['id']}", "status": "booked"}):
                fhir.update({**appt, "status": "cancelled"})
            fhir.update({**fhir.read("Slot", slot["id"]), "status": "free"})
    return schedules


@pytest.fixture
def fhir_gateway(seeded):
    gw = FhirScheduleGateway(FhirClient(FHIR_URL))
    set_gateway(gw)
    yield gw
    set_gateway(None)


@pytest.fixture
def practice(db_session):
    p = Practice(id=PRACTICE_ID, name="FHIR Test Dental", timezone=TZ, phone="+15550990001")
    db_session.add(p)
    db_session.commit()
    return p


def booking(when: datetime, name="Dana Lee", phone="813 555 0142", service="cleaning") -> BookAppointmentInput:
    return BookAppointmentInput(practice_id=PRACTICE_ID, caller_name=name, callback_number=phone,
                                service=service, requested_slot=when)


def get(url: str, **params) -> dict:
    return httpx.get(f"{FHIR_URL}/{url}", params=params,
                     headers={"Accept": "application/fhir+json", "Cache-Control": "no-cache"}).json()


# ── Availability ─────────────────────────────────────────────────────────────
def test_slots_come_from_the_seeded_schedules(fhir_gateway, practice):
    slots = fhir_gateway.slots(practice, at(0), at(0, day=8))  # one local day

    assert len(slots) == 2 * 16  # two practitioners, 9:00-17:00 in 30-minute slots
    assert {s.practitioner_name for s in slots} == {"Dr. Mei Chen", "Sam Rivera"}
    assert slots[0].start == at(9) and slots[0].status == "free"


def test_check_availability_offers_three_and_honours_the_service(fhir_gateway, practice, db_session):
    result = check_availability(db_session, CheckAvailabilityInput(
        practice_id=PRACTICE_ID, date_range=DateRange(start_date=MONDAY, end_date=date(2030, 1, 8)), service="cleaning",
    ))

    assert len(result.slots) == 3
    assert {s.practitioner for s in result.slots} == {"Sam Rivera"}  # cleanings are the hygienist's


# ── Booking: one transaction, Appointment + Slot ─────────────────────────────
def test_booking_creates_a_fhir_appointment_and_marks_the_slot_busy(fhir_gateway, practice, db_session):
    call = start_call(db_session, PRACTICE_ID, "+18135550142")
    [before] = [s for s in fhir_gateway.slots(practice, at(9, 30), at(10)) if "cleaning" in s.services]

    result = book_appointment(db_session, booking(at(9, 30)), call_id=call.id)

    appt = get(f"Appointment/{result.fhir_appointment_id}")
    assert appt["status"] == "booked"
    actors = [p["actor"]["reference"].split("/")[0] for p in appt["participant"]]
    assert actors == ["Patient", "Practitioner", "Location"]
    assert appt["serviceType"][0]["coding"][0]["code"] == "cleaning"
    assert appt["identifier"] == [{"system": f"{SID}/call", "value": str(call.id)}]  # links back to our Call
    slot = get(appt["slot"][0]["reference"])
    assert slot["id"] == before.id
    assert (slot["status"], int(slot["meta"]["versionId"])) == ("busy", int(before.version) + 1)  # exactly one write
    # The DoD query: it's on the day's appointment list.
    on_the_day = get("Appointment", date="2030-01-07", status="booked")
    assert result.fhir_appointment_id in [e["resource"]["id"] for e in on_the_day.get("entry", [])]
    # And the Postgres shadow row points at it.
    assert db_session.get(Appointment, result.appointment_id).fhir_appointment_id == result.fhir_appointment_id
    # The patient is stored with an E.164 phone, so caller-ID lookup finds them.
    patient = get(appt["participant"][0]["actor"]["reference"])
    assert patient["telecom"][0]["value"] == "+18135550142"


def test_a_returning_caller_is_the_same_patient(fhir_gateway, practice, db_session):
    first = book_appointment(db_session, booking(at(10), name="Robin Returning", phone="+1 (813) 555-0177"),
                             call_id=start_call(db_session, PRACTICE_ID, "x").id)
    second = book_appointment(db_session, booking(at(10, 30), name="Robin Returning", phone="813-555-0177"),
                              call_id=start_call(db_session, PRACTICE_ID, "y").id)

    patients = {get(f"Appointment/{r.fhir_appointment_id}")["participant"][0]["actor"]["reference"]
                for r in (first, second)}
    assert len(patients) == 1  # matched by E.164 phone + family name, not duplicated


# ── The race: two callers, one slot ──────────────────────────────────────────
def test_two_concurrent_bookings_of_one_slot_one_wins_one_gets_the_next_time(seeded, practice, tmp_path):
    """Both threads read the slot (version 1), then a barrier releases their
    transactions at the same moment. HAPI accepts one and refuses the other
    with 409; the loser's caller hears the next open time."""
    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}", connect_args={"timeout": 15})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(Practice(id=PRACTICE_ID, name="FHIR Test Dental", timezone=TZ, phone="+15550990001"))
        db.commit()
        calls = [start_call(db, PRACTICE_ID, f"+1813555020{i}").id for i in (1, 2)]

    gateway = FhirScheduleGateway(FhirClient(FHIR_URL))
    barrier = threading.Barrier(2)
    real_book = gateway.book

    def book_together(*args, **kwargs):
        barrier.wait(timeout=10)  # both have read version 1 by now
        return real_book(*args, **kwargs)
    gateway.book = book_together
    set_gateway(gateway)

    outcomes: dict[int, object] = {}

    def caller(call_id: int, name: str):
        with Session() as db:
            try:
                outcomes[call_id] = book_appointment(db, booking(at(11), name=name, phone=f"+1813555{call_id:04d}"),
                                                     call_id=call_id)
            except ToolError as exc:
                outcomes[call_id] = exc

    threads = [threading.Thread(target=caller, args=(c, n)) for c, n in zip(calls, ("Ana Alpha", "Ben Beta"))]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        set_gateway(None)

    wins = [o for o in outcomes.values() if not isinstance(o, Exception)]
    losses = [o for o in outcomes.values() if isinstance(o, ToolError)]
    assert len(wins) == 1 and len(losses) == 1, outcomes
    assert "was just taken by someone else, so it wasn't booked" in str(losses[0])
    assert "The next open time is" in str(losses[0])
    # Exactly one booked Appointment holds the slot; the loser's write left nothing behind.
    slot_id = get(f"Appointment/{wins[0].fhir_appointment_id}")["slot"][0]["reference"]
    holders = get("Appointment", slot=slot_id, status="booked").get("entry", [])
    assert len(holders) == 1
    with Session() as db:
        assert db.query(Appointment).count() == 1


# ── Reschedule and cancel: both sides move together ──────────────────────────
def test_reschedule_and_cancel_update_fhir_and_postgres(fhir_gateway, practice, db_session):
    call = start_call(db_session, PRACTICE_ID, "+18135550199")
    booked = book_appointment(db_session, booking(at(13), name="Casey Mover", phone="813 555 0199"), call_id=call.id)
    old_slot = get(f"Appointment/{booked.fhir_appointment_id}")["slot"][0]["reference"]

    moved = reschedule_appointment(db_session, RescheduleAppointmentInput(
        appointment_id=booked.appointment_id, new_slot=at(14, day=8), reason="work conflict",
    ), practice_id=PRACTICE_ID)

    old = get(f"Appointment/{booked.fhir_appointment_id}")
    assert old["status"] == "cancelled" and "rescheduled" in old["cancelationReason"]["text"]
    assert get(old_slot)["status"] == "free"
    new = get(f"Appointment/{moved.fhir_appointment_id}")
    assert new["status"] == "booked" and get(new["slot"][0]["reference"])["status"] == "busy"
    assert db_session.get(Appointment, booked.appointment_id).status == AppointmentStatus.rescheduled

    cancel_appointment(db_session, moved.appointment_id, practice_id=PRACTICE_ID)

    assert get(f"Appointment/{moved.fhir_appointment_id}")["status"] == "cancelled"
    assert get(new["slot"][0]["reference"])["status"] == "free"
    assert db_session.get(Appointment, moved.appointment_id).status == AppointmentStatus.cancelled


def test_patients_are_matched_only_within_the_practice(fhir_gateway, practice, db_session, seeded):
    """Same name and phone, two practices: two different Patients. A live run
    matched a Sunshine Dental caller to a test practice's patient before
    matching was scoped by managingOrganization."""
    from scripts.seed_fhir import PracticeSeed, build_seed_bundle, seed

    other_id = PRACTICE_ID + 1
    with FhirClient(FHIR_URL) as fhir:
        seed(fhir, build_seed_bundle(PracticeSeed(other_id, "Other FHIR Dental", "+15550990002", TZ, None), MONDAY, days=1))
    db_session.add(Practice(id=other_id, name="Other FHIR Dental", timezone=TZ, phone="+15550990002"))
    db_session.commit()

    ours = book_appointment(db_session, booking(at(15), name="Sam Shared", phone="813 555 0166"),
                            call_id=start_call(db_session, PRACTICE_ID, "a").id)
    theirs = book_appointment(db_session, BookAppointmentInput(
        practice_id=other_id, caller_name="Sam Shared", callback_number="813 555 0166", service="cleaning",
        requested_slot=at(15)), call_id=start_call(db_session, other_id, "b").id)

    def patient_of(r):
        return get(get(f"Appointment/{r.fhir_appointment_id}")["participant"][0]["actor"]["reference"])
    ours_p, theirs_p = patient_of(ours), patient_of(theirs)
    assert ours_p["id"] != theirs_p["id"]
    assert ours_p["managingOrganization"]["reference"] != theirs_p["managingOrganization"]["reference"]
    # Clean up the other practice's booking so the next session starts free.
    cancel_appointment(db_session, theirs.appointment_id, practice_id=other_id)
