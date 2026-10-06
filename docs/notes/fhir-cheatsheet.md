# FHIR cheatsheet

FHIR R4 as served by the HAPI FHIR JPA server in `docker-compose.yaml`
(`hapiproject/hapi:v8.12.0-1`, its own Postgres). Base URL
`http://localhost:8090/fhir`. Client: [`app/fhir/client.py`](../../app/fhir/client.py).
Everything below was run against that server; responses are the ones it
actually gave.

```bash
docker compose up -d hapi          # ready in ~25 s
curl localhost:8090/fhir/metadata  # the CapabilityStatement
```

## What the server claims (`GET /metadata`)

- `CapabilityStatement`: `kind: instance`, software **HAPI FHIR Server
  8.12.0**, `fhirVersion` **4.0.1** (R4).
- **146 resource types**, each with `create`, `read`, `vread`, `update`,
  `patch`, `delete`, `history-instance`, `history-type` and `search-type`.
  `versioning: versioned-update`; conditional create, update and delete are
  supported.
- System level: `transaction` and `history-system`, plus admin operations
  (`$expunge`, `$reindex`, `$meta`, …).
- Formats: JSON, XML, Turtle. Patch: JSON Patch, XML Patch, FHIR patch.
- **Security: none declared.** It's an open server: anyone who can reach
  :8090 can read and write everything. Fine on a laptop; never expose it.
- It lists search parameters per resource (Patient: 33, Appointment: 26), and
  those are the *only* ones it accepts. An unknown one is a 400, not
  silently ignored.

## CRUD walkthrough (curl)

```bash
B=http://localhost:8090/fhir
curl -i -X POST $B/Patient -H 'Content-Type: application/fhir+json' \
  -d '{"resourceType":"Patient","name":[{"family":"Smith","given":["Jane"]}],"birthDate":"1985-04-12",
       "telecom":[{"system":"phone","value":"+1-813-555-0199"}]}'
#  -> 201, Location: …/Patient/1000/_history/1, ETag: W/"1"

curl $B/Patient/1000                      # read: meta.versionId "1"
curl "$B/Patient?family=Smith"            # search: Bundle type=searchset, entry[].search.mode = "match"
curl -i -X PUT $B/Patient/1000 -H 'Content-Type: application/fhir+json' \
  -d '{"resourceType":"Patient","id":"1000","name":[{"family":"Smith","given":["Jane"]}],
       "address":[{"city":"Tampa","state":"FL"}]}'
#  -> 200, ETag: W/"2"; meta.versionId "2"
curl $B/Patient/1000/_history/1           # vread: version 1 is still there, phone and all
curl $B/Patient/1000/_history             # history Bundle: v2 (PUT), v1 (POST)
```

- **`meta.versionId` goes up by one on every update**, and `ETag: W/"n"`
  mirrors it. `meta.lastUpdated` moves too.
- **PUT replaces the whole resource.** The phone number I didn't resend was
  gone in v2, and still present in v1. To change one field, either read,
  modify and PUT the whole thing, or use PATCH.
- **Optimistic locking:** `PUT` with `If-Match: W/"1"` when v2 is current
  → **409** `HAPI-0550: HAPI-0989: … is not the current version`. Use it
  whenever two writers (staff UI, agent) can touch the same record.
- **Deleted is not the same as never existed:** read after DELETE → **410**,
  not 404. History keeps the old versions.

## The seven resources a dental front desk needs

Searches below were verified against a linked test dataset (created in one
transaction). Replace ids with yours.

### 1. Patient: the person being treated
- **Key fields:** `identifier` (MRN, practice id), `name[].family/given`,
  `birthDate`, `gender`, `telecom[]` (`system` phone/email, `value`, `use`),
  `address[]`, `managingOrganization` → Organization, `active`.
- **Search:** `Patient?family=smithfield&birthdate=1985-04-12`: name plus
  date of birth, the identity check a front desk does on the phone. String
  search is case-insensitive and matches from the start.

### 2. Practitioner: a clinician (dentist, hygienist)
- **Key fields:** `name[]` (with `prefix`, e.g. "Dr."), `qualification[].code`
  (DDS, RDH), `telecom[]`, `identifier` (e.g. NPI), `active`. Their role *at
  a practice* is a separate resource, PractitionerRole.
- **Search:** `Practitioner?name=chenwick` (`name` searches family, given and
  prefix together).

### 3. Organization: the practice
- **Key fields:** `name`, `identifier`, `type`, `telecom[]`, `address[]`,
  `partOf` → Organization (a location of a group practice), `active`.
- **Search:** `Organization?name=sunshine%20dental` (spaces URL-encoded).

### 4. Schedule: whose time can be booked
- **Key fields:** `actor[]` → Practitioner / Location / … (whose calendar),
  `serviceType[]`, `planningHorizon` (start/end of the bookable period),
  `active`.
- **Search:** `Schedule?actor=Practitioner/1012`.

### 5. Slot: one bookable piece of a Schedule
- **Key fields:** `schedule` → Schedule, `status` (`free` | `busy` |
  `busy-unavailable` | `busy-tentative` | `entered-in-error`), `start`, `end`,
  `serviceType[]`. This app's `check_availability` computes slots from
  business hours; in FHIR they're stored rows that an EHR publishes.
- **Search:** `Slot?schedule=Schedule/1014&status=free&start=ge2026-10-05&start=lt2026-10-06`:
  free slots on one day. `ge`/`lt` are date prefixes, and repeating a
  parameter ANDs the conditions.

### 6. Appointment: a booked visit
- **Key fields:** `status` (`proposed` | `pending` | `booked` | `arrived` |
  `fulfilled` | `cancelled` | `noshow` | …), `start`, `end`, `serviceType[]`,
  `participant[]` (`actor` → Patient/Practitioner/Location, `status`
  accepted/declined/tentative/needs-action), `slot[]` → Slot, `reasonCode`.
  The patient is a participant, not a top-level field.
- **Search:** `Appointment?patient=Patient/1013&date=ge2026-10-01&status=booked`
  (this patient's upcoming booked visits). Add
  `&_include=Appointment:practitioner` to get the dentist in the same Bundle,
  with `search.mode: "include"`, so it isn't counted as a match.

### 7. Coverage: the patient's insurance
- **Key fields:** `status` (`active` | `cancelled` | …), `beneficiary` →
  Patient (who's covered), `subscriber`/`policyHolder`, `subscriberId`,
  `payor[]` (the insurer), `class[]` (group, plan: `type` + `value`),
  `period`.
- **Search:** `Coverage?beneficiary=Patient/1013&status=active`: "do you
  take my insurance?" starts from what's on file.

**Next ones to learn:** Encounter (a visit that happened), Condition,
Procedure (with ADA dental codes), MedicationRequest (prescriptions: the
refill case), Location, PractitionerRole, and the two containers everything
travels in: Bundle and OperationOutcome.

## Search: things that bit

- **Encode `+`.** `Patient?phone=+1-813-555-0199` found nothing: the `+`
  arrived as a space. `%2B1-813-555-0199` found the patient. (httpx encodes
  params itself, so the client is safe; hand-written URLs aren't.)
- **Token search is exact.** Stored `+1-813-555-0199` is **not** found by
  `+18135550199`, the E.164 form Twilio gives as caller ID. For caller-ID
  lookup, store numbers in E.164 when writing.
- **Don't trust `total` right after a write.** HAPI reuses the results of an
  identical search for a while. After a DELETE, the same search returned no
  entries but still `total: 1`. With `Cache-Control: no-cache` it was
  correct. The client sends that on every search and returns entries, never
  `total`.
- **Paging:** 20 per page by default (`_count` to change). Further pages
  come from the `link` with `relation: "next"`; the client follows them.
- **Empty is not an error:** a search that matches nothing is **200** with a
  Bundle of `total: 0` and no `entry` at all (not an empty list).

## Transactions

`POST /` with `{"resourceType":"Bundle","type":"transaction","entry":[…]}`,
each entry with `request.method`/`url`.
- **All or nothing:** one entry with a bad reference → **400**, and the valid
  Patient in the same Bundle was *not* created.
- **Entries reference each other before they have ids,** via `fullUrl:
  "urn:uuid:<uuid>"`. HAPI rewrote the Appointment's
  `participant.actor.reference` from the urn to the new `Patient/1008`.
- The response is a `transaction-response` Bundle, one entry per request in
  order, with `response.status` ("201 Created") and `response.location`.

## Errors → this app's HTTP semantics

What HAPI answered, and what `app/fhir/client.py` turns it into. `api_status`
is what our API returns when a FHIR call fails underneath one of our
endpoints (`app/main.py` registers the handler; the body carries HAPI's
codes, never its diagnostics text, which can echo patient data).

| Situation | HAPI | Exception | Our API |
|---|---|---|---|
| Search matches nothing | 200, empty Bundle | none: `[]` | 200 `[]` |
| Unknown id / unknown resource type | 404 `HAPI-2001` / `HAPI-0302` | `FhirNotFound` | 404 |
| Deleted resource | **410** | `FhirGone` | 404 (don't reveal a record existed) |
| Reference to a missing resource | **400** `HAPI-1094 … specified in path: Encounter.subject` | `FhirInvalidReference` | **422**: bad data submitted |
| Unknown search param, invalid code value, wrong `resourceType`, malformed JSON | 400 `HAPI-0524` / `HAPI-0450` | `FhirBadRequest` | 500: we built a bad request, our bug |
| Stale `If-Match` | **409** (not 412) `HAPI-0550: HAPI-0989` | `FhirConflict` | 409 |
| Server down / timeout / 5xx | connect error / 5xx | `FhirUnavailable` | 503 |

Two notes:
- **Send `Accept: application/fhir+json`.** Without it, the malformed-JSON
  error didn't come back as JSON. With it, every error is an
  `OperationOutcome` (`issue[].severity/code/diagnostics`).
- **Validation is parse-level only by default.** Bad JSON, unknown elements
  and invalid code values are rejected, but profiles aren't checked unless
  configured. Missing fields that a *profile* (e.g. US Core) would require
  go in.

## Using the client

```python
from app.fhir.client import FhirClient, FhirNotFound

with FhirClient() as fhir:                          # settings.fhir_base_url
    p = fhir.create({"resourceType": "Patient", "name": [{"family": "Smith"}]})
    p = fhir.read("Patient", p["id"])
    p["birthDate"] = "1985-04-12"
    p = fhir.update(p, if_match=p["meta"]["versionId"])  # FhirConflict if someone else wrote first
    smiths = fhir.search("Patient", {"family": "Smith"})  # list of resources, all pages
    result = fhir.transaction(bundle)
```

It's synchronous (like the DB layer); call it via `asyncio.to_thread` from
async code. It returns plain dicts. `fhir.resources` typed models were left
out: HAPI already validates, and they'd pay off only once the app *builds*
many complex resources.

## How this app's data lines up

| This app | FHIR |
|---|---|
| `Practice` | Organization (+ Location) |
| business hours → computed slots | Schedule + Slot |
| `Lead` (caller name, number) | Patient (once identified) |
| `Appointment` | Appointment (patient and dentist as participants, `slot` → Slot) |
| "Service: cleaning" in `Lead.notes` | `Appointment.serviceType` |
| `Call` + transcript | Encounter / Communication (if it has to live in the EHR at all) |
