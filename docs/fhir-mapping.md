# FHIR mapping

Everything the SQL schema (`app/models/models.py`) knows about scheduling,
as standard FHIR R4 resources, and which store owns what. Seeded into HAPI
by [`scripts/seed_fhir.py`](../scripts/seed_fhir.py); resource basics are in
[`notes/fhir-cheatsheet.md`](notes/fhir-cheatsheet.md).

## Source of truth: FHIR for scheduling, Postgres for calls

**Decision.** The **FHIR server is the system of record for scheduling**:
practices, practitioners, availability (Schedule/Slot), appointments, and
patient identity. **Postgres keeps what only this app produces**: calls,
leads, transcripts, latency metrics. Dialog state stays in Redis.

**Why.** Ask which system the practice actually runs on. It's their practice
management system or EHR (in production, reached via FHIR; locally, HAPI
stands in for it). That is where the front desk books at the counter, where
the hygienist's calendar lives, and where the dentist sees tomorrow's list.
The voice agent is *one more writer* into that calendar, not the calendar.

- **One schedule, one double-booking check.** Two copies of a calendar drift,
  and a slot the front desk books at 10:02 must be unavailable to the agent
  at 10:03. That only holds if the check happens in the store everyone
  writes to.
- **Interoperability is earned here.** If our Postgres held appointments, "we
  integrate with any FHIR-capable system" would mean a sync job. With FHIR
  as the record, it means pointing `FHIR_BASE_URL` at their server.
- **Call data doesn't belong in an EHR.** Transcripts of every call, failed
  turns, latency rows and unidentified callers are operational data about
  *our* product, much of it about people who never become patients.
  Pushing it into a clinical record would be noise at best and a privacy
  problem at worst.

**How it's built** (module 5.3; code in `app/scheduling/`):

| Agent step | Before (Postgres) | Now (FHIR is the record) |
|---|---|---|
| `check_availability` | computed slots from `business_hours`, minus booked rows | `GET /Slot?schedule=<the practice's Schedules>&status=free&start=ge…&start=lt…`. At most **three** times are offered, spread across days, and on the right calendar for the service ("cleaning" → hygienist) |
| Confirmation gate | Redis DialogState | **unchanged**. It reads the same free Slot, so the read-back can't promise a time that's gone |
| `book_appointment` | `INSERT` Lead + Appointment | one **transaction**: Patient (reused if this practice already has one with that phone and family name, else created), `POST Appointment` (status `booked`; Patient, Practitioner and Location as participants; `slot` → Slot; our call id as an identifier), and `PUT Slot` (status `busy`) with `If-Match: W/"<version we read>"` |
| The race | n/a | someone booked the Slot since we read it → HAPI **409**, *nothing* written → "10 AM was just taken by someone else, so it wasn't booked. The next open time is 10:30 AM…" |
| Postgres `appointments` | the booking itself | the **shadow**: which call booked which FHIR Appointment (`fhir_appointment_id`), status `confirmed` |
| reschedule / cancel | Postgres rows only | one FHIR transaction each: old Appointment `cancelled` + its Slot `free` (+ the new booking), then the shadow rows. `cancel_appointment` exists but isn't an agent tool yet: cancelling needs its own confirmation step |
| FHIR unreachable | n/a | **don't book.** The agent is told to offer a callback (the Lead with the caller's details is already saved). Never book locally and sync later: that's how double bookings happen |

Verified in a live run through the text harness (real Claude, Redis and
HAPI): the caller asked for a cleaning on Tuesday at 10. The agent read it
back, the caller said yes, and HAPI had `Appointment/1350` (`booked`;
Patient, Practitioner/Sam Rivera the hygienist, Location) with
`Slot/1131` now `busy` at version 2, listed by `GET
/Appointment?date=2026-10-06&status=booked`.

**Costs, honestly:**
- **Every availability check and booking is a network call to someone
  else's system.** Locally that's cheap: the week's slots (80) took 24 ms
  p50 and one day 7 ms. A real EHR API is slower, rate-limited and sometimes
  down, and its latency lands inside the caller's wait (see
  `notes/latency-budget.md`).
- **Not every practice system publishes Slots.** Many expose free time only
  through an operation or a proprietary API. The mapping holds; the
  `check_availability` adapter may need a second implementation.
- **Patient matching becomes real.** Booking needs a Patient resource, and a
  phone caller gives a name and a number. Matching is on E.164 phone (exact
  token match, see the cheatsheet) plus family name, **within the practice**
  (`organization=` / `managingOrganization`). Two candidates → create a new
  Patient rather than guess. The scope came from a live run: before it, a
  Sunshine Dental caller was matched to a *test practice's* patient with the
  same name and number. On a FHIR server shared by several practices,
  that's a cross-tenant leak.
- **FHIR URLs are patient data.** `Patient?phone=…&family=Lee` showed up in
  the request logs. The scrubber masked the phone but not the name, so the
  HTTP client's request-line logging is off (`app/fhir/client.py`).

## Mapping

Identifiers use systems this app owns, under
`http://fde-voice-agent.example/sid/` (`.example` is reserved for exactly
this). Every seeded resource carries one, which is what makes the seed
idempotent.

| SQL (`models.py`) | FHIR | Notes |
|---|---|---|
| `Practice` | **Organization** + **Location** | Organization is the business; Location is the place appointments happen |
| *(dentist, none in SQL)* | **Practitioner** + **PractitionerRole** | Role = this practitioner at this practice, with specialty and hours |
| `business_hours` + `SLOT_MINUTES` (computed slots) | **Schedule** (per practitioner) + **Slot** (30 min, `free`/`busy`) | Slots are stored rows, not computed |
| `Lead` (caller) | **Patient**, once identified | Unidentified callers stay Leads; a callback request could be a Task |
| `Appointment` | **Appointment** | Patient, Practitioner and Location as participants; `slot` → Slot |
| `Call`, `TranscriptTurn`, `CallMetric`, DialogState | *not mapped*: Postgres / Redis | Operational data of this app (see above) |

### Practice → Organization + Location

| SQL | Organization | Location |
|---|---|---|
| `id` | `identifier` `sid/practice\|4` | `identifier` `sid/location\|4`; `managingOrganization` → Organization |
| `name` | `name` | `name` |
| `phone` | `telecom[phone]` | `telecom[phone]` |
| `timezone` | | extension `http://hl7.org/fhir/StructureDefinition/timezone` (`valueCode` IANA zone). R4 Location has no timezone field |
| `business_hours` `{"mon": ["09:00","17:00"], "sat": null}` | | `hoursOfOperation[] {daysOfWeek, openingTime, closingTime}`: same data, grouped by window |
| | `type` = `prov` (Healthcare Provider) | `status: active`, `mode: instance` |

### Dentist → Practitioner + PractitionerRole

| Field | Practitioner | PractitionerRole |
|---|---|---|
| who | `name` (family, given, prefix "Dr."), `qualification.code.text` (DDS, RDH), `identifier` `sid/practitioner\|4-chen` | `practitioner` →, `organization` →, `location[]` → |
| what they do | | `code`: NUCC provider taxonomy (`1223G0001X` Dentist, General Practice; `124Q00000X` Dental Hygienist) |
| when | | `availableTime[]` from the practice's hours |

A real deployment would add the NPI as an `identifier`.

### Availability → Schedule + Slot

| Concept | FHIR |
|---|---|
| whose calendar | `Schedule.actor[]` → Practitioner **and** Location |
| what can be booked | `Schedule.serviceType[]` / `Slot.serviceType[]`: `cleaning` (hygienist), `exam` and `filling` (dentist), local code system `…/CodeSystem/service-type`. A real system would use ADA CDT procedure codes |
| bookable period | `Schedule.planningHorizon` (the seeded week) |
| one 30-minute opening | `Slot {schedule →, start, end, status}`, with `start`/`end` in UTC (`2026-10-05T13:00:00Z` = 9:00 EDT) |
| taken / open | `Slot.status`: `free` → `busy` when booked (`busy-unavailable` for blocked time) |

### Appointment ↔ Appointment

| SQL `Appointment` | FHIR `Appointment` |
|---|---|
| `id` | `identifier` (ours), plus the server id |
| `requested_slot` | `start`, plus `end` (= start + 30 min) and `slot[]` → Slot |
| `lead → call → practice` | `participant[]`: `actor` Patient, Practitioner, Location, each with `status` accepted / needs-action |
| `"Service: cleaning"` in `Lead.notes` | `serviceType[]` (a real field, finally) |
| `status` pending / confirmed / cancelled | `status` `pending` / `booked` / `cancelled` |
| `status` rescheduled (old row) + new row | old: `cancelled` with `cancelationReason`; new: a fresh Appointment |
| `confirmation` | `comment` (or an `identifier` if it's a confirmation code) |
| Lead `name` / `callback_number` | on the **Patient**: `name`, `telecom[phone]` in E.164 |

## The seed

```bash
docker compose up -d hapi
python -m scripts.seed_fhir --practice-id 4            # week of next Monday
python -m scripts.seed_fhir --practice-id 4 --week-of 2026-10-05 --fhir-url http://localhost:8091/fhir
```

It reads Practice 4 from Postgres and builds **one transaction Bundle**: 168
entries (Organization, Location, 2 Practitioners, 2 PractitionerRoles,
2 Schedules, 160 Slots = 2 × 5 weekdays × 16). Every entry is a conditional
create, `ifNoneExist: identifier=<system>|<value>`. Identifiers name *what*
the thing is, e.g. `sid/slot|4-chen-2026-10-05T13:00:00Z`.

**Verified on a fresh HAPI** (a throwaway `hapiproject/hapi:v8.12.0-1`
container, empty, port 8091):

| | Run 1 | Run 2 |
|---|---|---|
| Created | 168 (all) | **0** |
| Already there | 0 | 168 |
| Server totals after | 1 Org, 1 Location, 2 Practitioner, 2 PractitionerRole, 2 Schedule, 160 Slot | **same** |

Also checked:
- Monday's first slot is `13:00Z` (9:00 EDT), and the last is 16:30–17:00
  local.
- Saturday and Sunday have no slots (closed in `business_hours`).
- **A slot booked as `busy` stays busy when the seed runs again.** A
  conditional create never overwrites, so a re-run can't undo a booking.

Unit tests: `tests/test_seed_fhir.py`, covering slot math across a DST change,
closed days and custom hours, unique conditional identifiers, deterministic
bundles, and references resolving inside the bundle.

## Reading it back: this is `check_availability`

```bash
curl "http://localhost:8090/fhir/Slot?schedule=Schedule/1029&status=free&start=ge2026-10-05&start=lt2026-10-12" \
  -H "Accept: application/fhir+json" -H "Cache-Control: no-cache"
```

```python
fhir.search("Slot", [("schedule", "Schedule/1029"), ("status", "free"),
                     ("start", "ge2026-10-05"), ("start", "lt2026-10-12")])
```

On the seeded server this returns 80 free slots per practitioner for the
week. Repeating `start` ANDs the two bounds, and `Cache-Control: no-cache`
keeps a just-booked slot from showing up stale.

**By service, across practitioners** (no need to know who does cleanings):
`Slot?service-type=http://fde-voice-agent.example/CodeSystem/service-type|cleaning&status=free&start=ge2026-10-05&start=lt2026-10-12`
returned 80 slots, all on the hygienist's schedule. The same query for
`exam` on one day returned the dentist's 16. (In a URL, write the `|` as
`%7C`.)

## Testing it: real HAPI, not recordings

Two layers:

- **The FHIR gateway** (`app/scheduling/fhir.py`) is tested against a **real
  HAPI** (`tests/test_fhir_scheduling.py`). The session seeds its own
  practice (id 990001, two days in 2030) and resets its slots to free, so
  reruns are independent. The tests skip when HAPI isn't up; in CI it runs
  as a service container from the same image.
- **Everything above it** (the tools, the confirmation gate, the voice and
  MCP paths, the harness; ~350 tests) runs offline against an in-memory
  fake with the same contract (`tests/scheduling_fake.py`): free/busy, a
  version per slot, `SlotTaken` on a stale version.

**Why HAPI and not `respx` recordings:** what has to be proven here is the
*server's* behaviour. A transaction is all-or-nothing, a stale `If-Match` is
refused, and two simultaneous bookings of one slot can't both succeed. A
recording only replays responses we already expected: it can't show a
rollback or a race. A recording would also silently go stale against a new
HAPI version; a real server can't.

**The concurrency test:** two threads, two callers, one slot. A barrier
holds both until each has read the slot (same version), then releases both
transactions at once. Result, 10 runs out of 10: one booking, one "just
taken, the next open time is …", and exactly one `booked` Appointment on
that slot.
