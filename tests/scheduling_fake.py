"""An in-memory ScheduleGateway for the agent's offline tests: the same
contract as app/scheduling/fhir.py's FhirScheduleGateway, without a server.

Slots are generated from the practice's business hours (the same
app.scheduling.slot_starts the FHIR seed uses), so every practice a test
creates "has a calendar". Each slot has a version that changes on every
write, and book() raises SlotTaken when the version it was given is stale,
exactly the FHIR If-Match behaviour. The FHIR gateway itself is tested
against a real HAPI in tests/test_fhir_scheduling.py.
"""

import itertools
import threading
from datetime import datetime, timedelta

from app.scheduling import SLOT_MINUTES, PatientInfo, SchedulingError, SlotRef, SlotTaken, slot_starts


class InMemoryScheduleGateway:
    PRACTITIONER = "Practitioner/fake-dentist"
    PRACTITIONER_NAME = "Dr. Test"
    SERVICES = ("cleaning", "exam", "filling")

    def __init__(self):
        self._lock = threading.Lock()
        self._busy: dict[tuple[int, datetime], str] = {}      # (practice, start) -> appointment id
        self._versions: dict[tuple[int, datetime], int] = {}
        self.appointments: dict[str, dict] = {}
        self._ids = itertools.count(1)

    def _version(self, key) -> str:
        return str(self._versions.get(key, 1))

    def _ref(self, practice, start: datetime) -> SlotRef:
        key = (practice.id, start)
        return SlotRef(
            id=f"{practice.id}-{start.isoformat()}", version=self._version(key), start=start,
            end=start + timedelta(minutes=SLOT_MINUTES), status="busy" if key in self._busy else "free",
            practitioner=self.PRACTITIONER, practitioner_name=self.PRACTITIONER_NAME,
            location=f"Location/fake-{practice.id}", services=self.SERVICES,
        )

    def slots(self, practice, start: datetime, end: datetime, *, free_only: bool = True) -> list[SlotRef]:
        days = (end.date() - start.date()).days + 2
        with self._lock:
            refs = [self._ref(practice, s)
                    for s in slot_starts(practice.timezone, practice.business_hours, start.date() - timedelta(days=1), days)
                    if start <= s < end]
        return [r for r in refs if not free_only or r.status == "free"]

    def mark_busy(self, practice, start: datetime) -> None:
        """Test helper: someone else (the front desk) booked this slot."""
        with self._lock:
            key = (practice.id, start)
            self._busy[key] = "booked-elsewhere"
            self._versions[key] = self._versions.get(key, 1) + 1

    def book(self, practice, slot: SlotRef, patient: PatientInfo, *, service: str,
             notes: str | None = None, call_id: int | None = None) -> str:
        with self._lock:
            key = (practice.id, slot.start)
            if key in self._busy or self._version(key) != slot.version:
                raise SlotTaken(f"slot {slot.id} is no longer at version {slot.version}")
            appointment_id = f"fake-appt-{next(self._ids)}"
            self._busy[key] = appointment_id
            self._versions[key] = self._versions.get(key, 1) + 1
            self.appointments[appointment_id] = {"status": "booked", "key": key, "patient": patient,
                                                 "service": service, "call_id": call_id}
            return appointment_id

    def _free(self, appointment_id: str) -> tuple:
        appt = self.appointments.get(appointment_id)
        if appt is None or appt["status"] != "booked":
            raise SchedulingError(f"No booked appointment {appointment_id}")
        appt["status"] = "cancelled"
        self._busy.pop(appt["key"], None)
        self._versions[appt["key"]] = self._versions.get(appt["key"], 1) + 1
        return appt

    def reschedule(self, practice, appointment_id: str, new_slot: SlotRef, *, reason: str | None = None) -> str:
        with self._lock:
            key = (practice.id, new_slot.start)
            if key in self._busy or self._version(key) != new_slot.version:
                raise SlotTaken(f"slot {new_slot.id} is no longer at version {new_slot.version}")
            old = self._free(appointment_id)
            new_id = f"fake-appt-{next(self._ids)}"
            self._busy[key] = new_id
            self._versions[key] = self._versions.get(key, 1) + 1
            self.appointments[new_id] = {**old, "status": "booked", "key": key}
            return new_id

    def cancel(self, practice, appointment_id: str) -> None:
        with self._lock:
            self._free(appointment_id)
