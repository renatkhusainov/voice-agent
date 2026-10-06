"""ScheduleGateway backed by a FHIR server: the practice's real scheduling
system (HAPI in docker-compose.yaml, seeded by scripts/seed_fhir.py).

  * Availability is a Slot search: the practice's Schedules (found via its
    Location), status=free, a start range. Cached per practice: Schedules
    and Practitioners don't change mid-call.
  * A booking is ONE transaction: [new Patient if needed] + POST Appointment
    + PUT Slot (status=busy) with `If-Match: W/"<version we read>"`. All or
    nothing. If anyone booked that Slot since we read it, HAPI answers 409
    and *nothing* is written (verified, including two concurrent bookings of
    one slot: exactly one wins). That 409 is SlotTaken.
  * Reschedule and cancel are transactions too: the old Appointment
    cancelled and its Slot freed, in the same unit as the new booking.

Identifiers and code systems match scripts/seed_fhir.py and
docs/fhir-mapping.md.
"""

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.fhir.client import FhirClient, FhirConflict, FhirError, FhirUnavailable
from app.scheduling import (
    PatientInfo,
    service_code,
    SchedulingError,
    SchedulingUnavailable,
    SlotRef,
    SlotTaken,
)

SID = "http://fde-voice-agent.example/sid"
SERVICE_TYPES = "http://fde-voice-agent.example/CodeSystem/service-type"

def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class _Schedule:
    def __init__(self, ref: str, practitioner: str | None, practitioner_name: str | None, location: str | None):
        self.ref, self.practitioner, self.practitioner_name, self.location = ref, practitioner, practitioner_name, location


class FhirScheduleGateway:
    def __init__(self, client: FhirClient | None = None):
        self._fhir = client or FhirClient()
        self._schedules: dict[int, dict[str, _Schedule]] = {}
        self._organizations: dict[int, str | None] = {}
        self._lock = threading.Lock()

    # ── Lookup ───────────────────────────────────────────────────────────
    def _schedules_for(self, practice_id: int) -> dict[str, _Schedule]:
        with self._lock:
            if practice_id in self._schedules:
                return self._schedules[practice_id]
        locations = self._call(self._fhir.search, "Location", {"identifier": f"{SID}/location|{practice_id}"})
        if not locations:
            raise SchedulingError(f"Practice {practice_id} isn't set up in the scheduling system")
        location = f"Location/{locations[0]['id']}"
        organization = locations[0].get("managingOrganization", {}).get("reference")
        schedules: dict[str, _Schedule] = {}
        for sched in self._call(self._fhir.search, "Schedule", {"actor": location, "active": "true"}):
            actors = [a["reference"] for a in sched.get("actor", [])]
            practitioner = next((a for a in actors if a.startswith("Practitioner/")), None)
            name = None
            if practitioner:
                p = self._call(self._fhir.read, "Practitioner", practitioner.split("/", 1)[1])
                n = (p.get("name") or [{}])[0]
                name = " ".join([*n.get("prefix", []), *n.get("given", []), n.get("family", "")]).strip() or None
            schedules[f"Schedule/{sched['id']}"] = _Schedule(f"Schedule/{sched['id']}", practitioner, name, location)
        with self._lock:
            self._schedules[practice_id] = schedules
            self._organizations[practice_id] = organization
        return schedules

    def _call(self, fn, *args, **kwargs):
        """FHIR errors -> scheduling errors, so tools.py never sees FHIR."""
        try:
            return fn(*args, **kwargs)
        except FhirConflict as exc:
            raise SlotTaken(str(exc)) from exc
        except FhirUnavailable as exc:
            raise SchedulingUnavailable(str(exc)) from exc
        except FhirError as exc:
            logger.warning("FHIR scheduling request failed: {} status={} codes={}", type(exc).__name__, exc.status, exc.codes)
            raise SchedulingError(str(exc)) from exc

    def _slot_ref(self, slot: dict, schedules: dict[str, _Schedule]) -> SlotRef:
        sched = schedules.get(slot["schedule"]["reference"])
        services = tuple(c["code"] for st in slot.get("serviceType", []) for c in st.get("coding", [])
                         if c.get("system") == SERVICE_TYPES)
        return SlotRef(
            id=slot["id"], version=slot["meta"]["versionId"], start=_parse(slot["start"]), end=_parse(slot["end"]),
            status=slot["status"], practitioner=sched.practitioner if sched else None,
            practitioner_name=sched.practitioner_name if sched else None,
            location=sched.location if sched else None, services=services, resource=slot,
        )

    def slots(self, practice: Any, start: datetime, end: datetime, *, free_only: bool = True) -> list[SlotRef]:
        schedules = self._schedules_for(practice.id)
        if not schedules:
            return []
        params = [("schedule", ",".join(schedules)), ("start", f"ge{_iso(start)}"), ("start", f"lt{_iso(end)}"),
                  ("_sort", "start"), ("_count", "200")]
        if free_only:
            params.append(("status", "free"))
        return [self._slot_ref(s, schedules) for s in self._call(self._fhir.search, "Slot", params)]

    # ── Writes ───────────────────────────────────────────────────────────
    def _patient(self, practice_id: int, patient: PatientInfo) -> tuple[dict, list[dict]]:
        """Reuse the one Patient *of this practice* with this phone and family
        name; otherwise a new one in the same transaction. Scoped by
        managingOrganization: on a FHIR server shared by several practices, a
        caller to one practice must never be matched to another's patient (a
        live run did exactly that before this scope existed). Two or more
        matches: create a new one rather than guess between people (staff can
        merge; a wrong merge is worse)."""
        self._schedules_for(practice_id)
        organization = self._organizations.get(practice_id)
        parts = patient.name.split()
        family = parts[-1] if parts else patient.name
        query = {"phone": patient.phone, "family": family}
        if organization:
            query["organization"] = organization
        matches = self._call(self._fhir.search, "Patient", query)
        if len(matches) == 1:
            return {"reference": f"Patient/{matches[0]['id']}", "display": patient.name}, []
        full_url = f"urn:uuid:{uuid.uuid4()}"
        resource = {"resourceType": "Patient",
                    "name": [{"text": patient.name, "family": family, "given": parts[:-1]}],
                    "telecom": [{"system": "phone", "value": patient.phone, "use": "mobile"}],
                    **({"managingOrganization": {"reference": organization}} if organization else {})}
        return ({"reference": full_url, "display": patient.name},
                [{"fullUrl": full_url, "resource": resource, "request": {"method": "POST", "url": "Patient"}}])

    @staticmethod
    def _slot_put(slot_resource: dict, status: str) -> dict:
        return {"resource": {**slot_resource, "status": status},
                "request": {"method": "PUT", "url": f"Slot/{slot_resource['id']}",
                            "ifMatch": f'W/"{slot_resource["meta"]["versionId"]}"'}}

    def _appointment_entries(self, slot: SlotRef, patient_ref: dict, *, service: str,
                             notes: str | None, call_id: int | None) -> list[dict]:
        code = service_code(service)
        service_type = {"text": service, **({"coding": [{"system": SERVICE_TYPES, "code": code}]} if code else {})}
        participants = [{"actor": patient_ref, "required": "required", "status": "accepted"}]
        participants += [{"actor": {"reference": ref}, "required": "required", "status": "accepted"}
                         for ref in (slot.practitioner, slot.location) if ref]
        appointment = {
            "resourceType": "Appointment", "status": "booked",
            "serviceType": [service_type], "start": _iso(slot.start), "end": _iso(slot.end),
            "slot": [{"reference": f"Slot/{slot.id}"}], "participant": participants,
            # Which of our calls booked it: the FHIR side links back to Postgres.
            **({"identifier": [{"system": f"{SID}/call", "value": str(call_id)}]} if call_id else {}),
            **({"comment": notes} if notes else {}),
        }
        return [{"fullUrl": f"urn:uuid:{uuid.uuid4()}", "resource": appointment,
                 "request": {"method": "POST", "url": "Appointment"}},
                self._slot_put(slot.resource, "busy")]

    def _transaction(self, entries: list[dict]) -> dict:
        return self._call(self._fhir.transaction, {"resourceType": "Bundle", "type": "transaction", "entry": entries})

    @staticmethod
    def _created_appointment_id(response: dict) -> str:
        for entry in response["entry"]:
            location = entry["response"].get("location", "")
            if location.startswith("Appointment/") and entry["response"]["status"].startswith("201"):
                return location.split("/")[1]
        raise SchedulingError("The scheduling system didn't return the new appointment")

    def book(self, practice: Any, slot: SlotRef, patient: PatientInfo, *, service: str,
             notes: str | None = None, call_id: int | None = None) -> str:
        patient_ref, patient_entries = self._patient(practice.id, patient)
        entries = patient_entries + self._appointment_entries(slot, patient_ref, service=service,
                                                              notes=notes, call_id=call_id)
        return self._created_appointment_id(self._transaction(entries))

    def _cancel_entries(self, appointment: dict, reason: str) -> list[dict]:
        entries = [{"resource": {**appointment, "status": "cancelled", "cancelationReason": {"text": reason}},
                    "request": {"method": "PUT", "url": f"Appointment/{appointment['id']}",
                                "ifMatch": f'W/"{appointment["meta"]["versionId"]}"'}}]
        for ref in appointment.get("slot", []):
            old_slot = self._call(self._fhir.read, "Slot", ref["reference"].split("/", 1)[1])
            entries.append(self._slot_put(old_slot, "free"))
        return entries

    def reschedule(self, practice: Any, appointment_id: str, new_slot: SlotRef, *, reason: str | None = None) -> str:
        old = self._call(self._fhir.read, "Appointment", appointment_id)
        patient_ref = next((p["actor"] for p in old.get("participant", [])
                            if p.get("actor", {}).get("reference", "").startswith("Patient/")), {"display": "patient"})
        service = (old.get("serviceType") or [{}])[0].get("text", "appointment")
        call_ids = [i["value"] for i in old.get("identifier", []) if i.get("system") == f"{SID}/call"]
        entries = self._cancel_entries(old, f"rescheduled: {reason}" if reason else "rescheduled")
        entries += self._appointment_entries(new_slot, patient_ref, service=service, notes=old.get("comment"),
                                             call_id=int(call_ids[0]) if call_ids else None)
        return self._created_appointment_id(self._transaction(entries))

    def cancel(self, practice: Any, appointment_id: str) -> None:
        old = self._call(self._fhir.read, "Appointment", appointment_id)
        self._transaction(self._cancel_entries(old, "cancelled by caller"))


__all__ = ["FhirScheduleGateway", "service_code"]
