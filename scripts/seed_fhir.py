"""Seed HAPI FHIR with one practice's scheduling world: Organization,
Location, two Practitioners (+ PractitionerRoles), a Schedule per
practitioner, and a week of 30-minute free Slots. See docs/fhir-mapping.md.

    python -m scripts.seed_fhir --practice-id 4
    python -m scripts.seed_fhir --practice-id 4 --week-of 2026-10-05 --fhir-url http://localhost:8091/fhir

The practice comes from Postgres (name, phone, timezone, business hours):
that's what our SQL schema knows. The practitioners don't exist in SQL, so
they're defined here.

**Idempotent.** One transaction Bundle. Every entry is a conditional create
(`ifNoneExist: identifier=<system>|<value>`), and each resource carries a
stable identifier derived from what it is ("practice 4, Dr. Chen, Monday
9:00"), never from a run. A second run creates nothing: HAPI answers 200
with the existing resource for each entry, and references between entries
(`urn:uuid:` fullUrls) resolve to those existing resources. It also never
*resets* anything: a Slot already booked as busy stays busy.
"""

import argparse
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.scheduling import DEFAULT_BUSINESS_HOURS, SLOT_MINUTES, slot_starts
from app.config import settings
from app.db import SessionLocal
from app.fhir.client import FhirClient
from app.models.models import Practice

# Identifier systems: URIs this app owns (`.example` is reserved for exactly
# this). A real deployment uses the practice's own MRN/NPI systems instead.
SID = "http://fde-voice-agent.example/sid"
SERVICE_TYPES = "http://fde-voice-agent.example/CodeSystem/service-type"
NUCC = "http://nucc.org/provider-taxonomy"
TIMEZONE_EXT = "http://hl7.org/fhir/StructureDefinition/timezone"
_UUID_NS = uuid.UUID("8d7c1f8e-5b2a-4c3e-9f10-2a6b4d0c7e11")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class PractitionerSeed:
    key: str
    family: str
    given: str
    prefix: str | None
    qualification: str
    role_code: str
    role_display: str
    services: tuple[str, ...]


PRACTITIONERS = (
    PractitionerSeed("chen", "Chen", "Mei", "Dr.", "DDS", "1223G0001X", "Dentist, General Practice",
                     ("exam", "filling")),
    PractitionerSeed("rivera", "Rivera", "Sam", None, "RDH", "124Q00000X", "Dental Hygienist",
                     ("cleaning",)),
)

SERVICE_DISPLAY = {"cleaning": "Cleaning", "exam": "Exam", "filling": "Filling"}


@dataclass(frozen=True)
class PracticeSeed:
    id: int
    name: str
    phone: str
    timezone: str
    business_hours: dict

    @classmethod
    def from_db(cls, practice: Practice) -> "PracticeSeed":
        return cls(practice.id, practice.name, practice.phone, practice.timezone,
                   practice.business_hours or DEFAULT_BUSINESS_HOURS)


def _identifier(kind: str, value: str) -> dict:
    return {"system": f"{SID}/{kind}", "value": value}


def _service(code: str) -> dict:
    return {"coding": [{"system": SERVICE_TYPES, "code": code, "display": SERVICE_DISPLAY[code]}],
            "text": SERVICE_DISPLAY[code]}


def _entry(resource: dict, kind: str, value: str) -> dict:
    """A conditional create keyed by the resource's own stable identifier."""
    ident = _identifier(kind, value)
    resource["identifier"] = [ident]
    return {
        "fullUrl": f"urn:uuid:{uuid.uuid5(_UUID_NS, f'{kind}/{value}')}",
        "resource": resource,
        "request": {"method": "POST", "url": resource["resourceType"],
                    "ifNoneExist": f"identifier={ident['system']}|{ident['value']}"},
    }


def _ref(entry: dict) -> dict:
    return {"reference": entry["fullUrl"]}


def _opening_hours(business_hours: dict) -> list[dict]:
    """business_hours {"mon": ["09:00", "17:00"], "sat": None} -> FHIR's
    hoursOfOperation / availableTime: one entry per distinct opening window."""
    by_window: dict[tuple[str, str], list[str]] = {}
    for day in DAYS:
        window = business_hours.get(day)
        if window:
            by_window.setdefault((window[0], window[1]), []).append(day)
    return [{"daysOfWeek": days, "openingTime": f"{o}:00", "closingTime": f"{c}:00"}
            for (o, c), days in by_window.items()]


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_seed_bundle(practice: PracticeSeed, week_of: date, *, days: int = 7,
                      practitioners: tuple[PractitionerSeed, ...] = PRACTITIONERS) -> dict:
    p = str(practice.id)
    hours = _opening_hours(practice.business_hours or DEFAULT_BUSINESS_HOURS)
    org = _entry({
        "resourceType": "Organization", "active": True, "name": practice.name,
        "type": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/organization-type",
                              "code": "prov", "display": "Healthcare Provider"}]}],
        "telecom": [{"system": "phone", "value": practice.phone, "use": "work"}],
    }, "practice", p)
    location = _entry({
        "resourceType": "Location", "status": "active", "mode": "instance", "name": practice.name,
        "telecom": [{"system": "phone", "value": practice.phone, "use": "work"}],
        "managingOrganization": _ref(org),
        "hoursOfOperation": hours,
        # Our Practice.timezone. R4 Location has no timezone field; this is the
        # standard extension for it (core field in later versions).
        "extension": [{"url": TIMEZONE_EXT, "valueCode": practice.timezone}],
    }, "location", p)
    entries = [org, location]

    slot_times = slot_starts(practice.timezone, practice.business_hours, week_of, days)
    horizon_end = datetime.combine(week_of + timedelta(days=days), time(0), tzinfo=ZoneInfo(practice.timezone))
    for seed in practitioners:
        key = f"{p}-{seed.key}"
        practitioner = _entry({
            "resourceType": "Practitioner", "active": True,
            "name": [{"family": seed.family, "given": [seed.given], **({"prefix": [seed.prefix]} if seed.prefix else {})}],
            "qualification": [{"code": {"text": seed.qualification}}],
        }, "practitioner", key)
        role = _entry({
            "resourceType": "PractitionerRole", "active": True,
            "practitioner": _ref(practitioner), "organization": _ref(org), "location": [_ref(location)],
            "code": [{"coding": [{"system": NUCC, "code": seed.role_code, "display": seed.role_display}]}],
            "availableTime": [{"daysOfWeek": h["daysOfWeek"], "availableStartTime": h["openingTime"],
                               "availableEndTime": h["closingTime"]} for h in hours],
        }, "practitioner-role", key)
        schedule = _entry({
            "resourceType": "Schedule", "active": True,
            "actor": [_ref(practitioner), _ref(location)],
            "serviceType": [_service(s) for s in seed.services],
            "planningHorizon": {"start": week_of.isoformat(), "end": _iso(horizon_end.astimezone(timezone.utc))},
            "comment": f"{seed.prefix + ' ' if seed.prefix else ''}{seed.given} {seed.family}, {SLOT_MINUTES}-minute slots",
        }, "schedule", key)
        entries += [practitioner, role, schedule]
        for start in slot_times:
            entries.append(_entry({
                "resourceType": "Slot", "status": "free", "schedule": _ref(schedule),
                "serviceType": [_service(s) for s in seed.services],
                "start": _iso(start), "end": _iso(start + timedelta(minutes=SLOT_MINUTES)),
            }, "slot", f"{key}-{_iso(start)}"))

    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def seed(fhir: FhirClient, bundle: dict) -> dict[str, dict[str, int]]:
    """Post the transaction; count created (201) vs already there (200) per type."""
    response = fhir.transaction(bundle)
    counts: dict[str, dict[str, int]] = {}
    for request, result in zip(bundle["entry"], response["entry"]):
        kind = request["resource"]["resourceType"]
        outcome = "created" if result["response"]["status"].startswith("201") else "existing"
        counts.setdefault(kind, {"created": 0, "existing": 0})[outcome] += 1
    return counts


def next_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) or 7)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.seed_fhir")
    parser.add_argument("--practice-id", type=int, required=True)
    parser.add_argument("--week-of", type=date.fromisoformat, default=None,
                        help="first day of the seeded week (default: next Monday)")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--fhir-url", default=settings.fhir_base_url)
    args = parser.parse_args()

    with SessionLocal() as db:
        practice = db.get(Practice, args.practice_id)
        if practice is None:
            print(f"No practice {args.practice_id} in Postgres", file=sys.stderr)
            return 1
        seed_practice = PracticeSeed.from_db(practice)
    week_of = args.week_of or next_monday()

    bundle = build_seed_bundle(seed_practice, week_of, days=args.days)
    with FhirClient(args.fhir_url) as fhir:
        counts = seed(fhir, bundle)
        print(f"Seeded {seed_practice.name} (practice {seed_practice.id}) into {args.fhir_url}, "
              f"week of {week_of} ({len(bundle['entry'])} entries in one transaction):")
        for kind, c in counts.items():
            print(f"  {kind:<17} created {c['created']:>3}   already there {c['existing']:>3}")

        # Read it back: this query is check_availability on FHIR.
        start, end = week_of.isoformat(), (week_of + timedelta(days=args.days)).isoformat()
        for seed_p in PRACTITIONERS:
            [schedule] = fhir.search("Schedule", {"identifier": f"{SID}/schedule|{seed_practice.id}-{seed_p.key}"})
            free = fhir.search("Slot", [("schedule", f"Schedule/{schedule['id']}"), ("status", "free"),
                                        ("start", f"ge{start}"), ("start", f"lt{end}"), ("_count", "200")])
            print(f"  GET /Slot?schedule=Schedule/{schedule['id']}&status=free&start=ge{start}&start=lt{end}"
                  f"  -> {len(free)} free ({seed_p.prefix + ' ' if seed_p.prefix else ''}{seed_p.family})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
