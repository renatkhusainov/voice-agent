# FHIR R5 — reading notes for module 5

Reading material for lesson 5.1, revised 2026-09-30 to **FHIR R5 (5.0.0)**,
verified against the R5 spec pages for Appointment and Location, and pointed
at this project's schema. It is **input**, not the
`docs/notes/fhir-cheatsheet.md` deliverable — write that one yourself after
you have real HAPI responses in front of you.

> **Which version to run.** The course lesson, HAPI's default, Epic's
> sandbox, US Core, and every US hospital you'll interview at are **R4**.
> R5 has essentially no production footprint in US healthcare in 2026.
> This file is written R5-primary because you asked for it, and **every
> element that differs carries an inline `R4:` note** showing what a
> default HAPI server will actually return. If you run HAPI as R4 (the
> lesson's instruction), read the `R4:` notes as the truth and the R5 text
> as "what the spec moved to." If you run HAPI as R5, the reverse.

Reading order for the 45 minutes: §0 (what changed), §1–§3 (what a resource
is), §4 (references), §5 (REST API), §6 (search), §7 (bundles), §8 (the
seven resources). §9–§12 are for building 5.2 and 5.3.

---

## 0. R4 → R5: the changes that touch this project

| Where | R4 | R5 | Matters because |
|---|---|---|---|
| `Appointment.cancelationReason` | one *l* | **`cancellationReason`** (two *l*'s) | the R4 spelling was a typo the spec lived with for years; R5 fixed it |
| `Appointment.subject` | — | **new** `Reference(Patient\|Group) 0..1` | the patient is a first-class field; no more digging through `participant[]` to find them |
| `Appointment.previousAppointment` / `originatingAppointment` / `replaces` | — | **new** | a first-class reschedule chain — your close-old-open-new pattern gets a native link |
| `Appointment.participant.required` | `code` (required \| optional \| information-only) | **`boolean`** | type change |
| `Appointment.participant.actor` types | Patient, Practitioner, PractitionerRole, RelatedPerson, Device, HealthcareService, Location | + **Group, CareTeam** | |
| `Appointment.reasonCode` + `reasonReference` | two elements | **`reason`** (CodeableReference) | new datatype, §3 |
| `Appointment.comment` | string | **`note`** (Annotation[]) | |
| `Appointment.serviceType`, `Slot.serviceType`, `Schedule.serviceType` | CodeableConcept | **CodeableReference(HealthcareService)** | JSON shape changes: `{"concept": {…}}` |
| `Appointment.class`, `cancellationDate`, `virtualService`, `account`, recurrence (`recurrenceTemplate`, `recurrenceId`, `occurrenceChanged`) | — | **new** | recurring appointments are native in R5 |
| `Location.physicalType` | | renamed **`form`** | |
| `Location.telecom` | ContactPoint[] | **`contact`** (ExtendedContactDetail[]) | |
| `Location.hoursOfOperation` | backbone (daysOfWeek, allDay, openingTime, closingTime) | **`Availability`** datatype | same information, different shape; `availabilityExceptions` folded in |
| `Organization.telecom` + `address` | two elements | **`contact`** (ExtendedContactDetail[]) | phone and address move one level down |
| `PractitionerRole.telecom` / `availableTime` / `notAvailable` / `availabilityExceptions` | | **`contact`** + **`availability`** (Availability) | |
| `Schedule.name` | — | **new** | |
| `Slot.appointmentType` | 0..1 | 0..* | |
| Timezone on Organization / Location | none | **still none** | only `Appointment.recurrenceTemplate.timezone` exists — for recurrence, not for a site |
| REST API, search grammar, Bundle/transaction rules | | **unchanged** in anything you'll use | |
| US Core / Epic / ONC | R4 | **still R4** | no US national profile targets R5 |

---

## 1. What FHIR is, in three sentences

FHIR (Fast Healthcare Interoperability Resources) is HL7's REST-and-JSON
standard for exchanging healthcare data. The unit is a **resource** — a
typed JSON document (Patient, Appointment, …) with a fixed structure, served
at a URL, linked to other resources by reference. A server exposes them
through a **RESTful API** (`GET /Patient/123`) with a standard **search**
grammar and a **Bundle** type for moving many resources at once.

**Versions.** R4 (4.0.1, 2019) is what US regulation (ONC certification,
USCDI, US Core) and the big EHR vendors run. R4B (4.3.0, 2022) was a
maintenance release. **R5 (5.0.0, March 2023)** is the current normative
spec and what this file describes; it cleaned up naming, added
`CodeableReference`, `Availability` and `ExtendedContactDetail`, and made
recurring appointments native. R6 is in ballot. A hospital in 2026 is on
R4; HAPI ships R4 by default and runs R5 on request.

**What FHIR is not.** It is not a database schema, not an auth standard
(that's SMART on FHIR, §11), and not a guarantee of interoperability — base
FHIR is deliberately permissive, and *profiles* (§11) are what tighten it.
"FHIR-compliant" without a named profile means "speaks the JSON shape."

---

## 2. Anatomy of a resource

Every resource has the same skeleton (unchanged R4 → R5):

```json
{
  "resourceType": "Patient",
  "id": "123",
  "meta": {
    "versionId": "2",
    "lastUpdated": "2026-09-30T14:02:11.318+00:00",
    "profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"]
  },
  "identifier": [ { "system": "urn:oid:2.16.840.1.113883.4.1", "value": "…" } ],
  "active": true,
  "name": [ { "family": "Doe", "given": ["Jane"] } ],
  "telecom": [ { "system": "phone", "value": "813-555-0142", "use": "mobile" } ],
  "birthDate": "1987-03-14"
}
```

**`id` is not `identifier`.** `id` is the server's logical id — assigned on
POST, meaningful only on that server, part of the URL. `identifier` is a
*business* identifier — an NPI, a chart number, a member ID — with a
`system` that says who issued it. A resource can have many identifiers and
exactly one id. Searching by `identifier` is the normal way to find a thing
you know from the outside world; `id` is a row key.

**`meta.versionId`** increments on every update. It comes back as the
`ETag` header (`W/"2"`) and is what you send in `If-Match` to do optimistic
locking (§5.4). `meta.lastUpdated` is the server's timestamp, not yours.

**Content type** is `application/fhir+json`. HAPI also accepts
`?_format=json`.

**Cardinality** is written `min..max`: `0..1`, `1..1`, `0..*`, `1..*`. In
base FHIR almost everything is `0..`; the `1..` elements are the ones you
cannot omit — `Slot.schedule`, `Slot.status`, `Slot.start`, `Slot.end`,
`Appointment.status`, `Appointment.participant`,
`Appointment.participant.status`, `Schedule.actor`.

**Choice types** are written `value[x]` in the spec and appear in JSON as
`valueString`, `valueDateTime`, etc. — one concrete name per instance.

---

## 3. Data types you will actually meet

### Primitives (unchanged)

| Type | Example | Notes |
|---|---|---|
| `string` | `"Jane"` | |
| `boolean` | `true` | |
| `code` | `"free"` | a string from a fixed set |
| `uri` / `url` | `"http://loinc.org"` | |
| `date` | `"2026-10-06"` | partial precision allowed |
| `dateTime` | `"2026-10-06T09:30:00-04:00"` | partial precision allowed; **if a time is present, an offset is required** |
| `instant` | `"2026-10-06T13:30:00.000Z"` | full precision **and** offset, always. `Slot.start/end`, `Appointment.start/end`, `meta.lastUpdated` |

The `dateTime` vs `instant` distinction is where your `localize_slot()`
convention meets a hard rule: a naive practice-local time is never valid
FHIR. Resolve to an offset before anything crosses the boundary.

### Complex types (unchanged)

```json
"name":    [ { "use": "official", "family": "Doe", "given": ["Jane", "M"], "prefix": ["Dr."] } ]
"telecom": [ { "system": "phone", "value": "813-555-0142", "use": "mobile", "rank": 1 } ]
"address": [ { "line": ["14088 Kite Ln"], "city": "Lithia", "state": "FL", "postalCode": "33547", "country": "US" } ]
"identifier": [ { "system": "http://hl7.org/fhir/sid/us-npi", "value": "1234567890" } ]
"period":  { "start": "2026-10-01T00:00:00Z", "end": "2026-12-31T23:59:59Z" }
```

- **HumanName** — `family` is a string, `given` is an *array*. `use`: official, usual, nickname, maiden, …
- **ContactPoint** — `system`: phone, fax, email, sms, url, pager, other. `use`: home, work, mobile, temp, old.
- **Address** — `line` is an array.
- **Identifier** — `system` + `value`; optional `type`.
- **Period** — `start`/`end`, both optional.
- **Annotation** — `{ "text": "…", "time": "…", "authorString": "…" }`. R5 uses it where R4 had bare `comment` strings (e.g. `Appointment.note`).

### Coding and CodeableConcept (unchanged)

```json
{
  "coding": [ { "system": "http://www.ada.org/cdt", "code": "D1110", "display": "Prophylaxis - adult" } ],
  "text": "Adult cleaning"
}
```

A **Coding** is one code from one code system: `system`, `code`, optional
`display`/`version`. A **CodeableConcept** is an array of Codings plus free
`text`. Every "what kind" element — `specialty`, `type`, `appointmentType`,
`cancellationReason` — is a CodeableConcept.

### New in R5 — the three datatypes you'll meet

**CodeableReference** — "a concept *or* a reference to a resource, or
both." Used for `serviceType` (Appointment, Slot, Schedule),
`Appointment.reason`, `Appointment.patientInstruction`.

```json
"serviceType": [
  { "concept": { "coding": [ { "system": "http://www.ada.org/cdt", "code": "D1110" } ], "text": "cleaning" } }
]
```

> **R4:** `serviceType` is a plain CodeableConcept — the `coding`/`text`
> object sits directly in the array, with no `concept` wrapper. This is the
> JSON change most likely to bite you if the primer and the server
> disagree.

**Availability** — working hours plus exceptions, in one type. Used for
`Location.hoursOfOperation` and `PractitionerRole.availability`.

```json
"hoursOfOperation": [
  {
    "availableTime": [
      { "daysOfWeek": ["mon", "tue", "wed", "thu", "fri"], "availableStartTime": "09:00:00", "availableEndTime": "17:00:00" }
    ],
    "notAvailableTime": [
      { "description": "Thanksgiving", "during": { "start": "2026-11-26", "end": "2026-11-27" } }
    ]
  }
]
```

> **R4:** `Location.hoursOfOperation` is a backbone with `daysOfWeek`,
> `allDay`, `openingTime`, `closingTime` directly on each entry, and
> exceptions go in a separate `availabilityExceptions` string. Same
> `mon…sun` day codes either way — they match your `business_hours` keys.

**ExtendedContactDetail** — a contact with a purpose, a name, telecoms, an
address, and a period. Used for `Organization.contact`, `Location.contact`,
`PractitionerRole.contact`.

```json
"contact": [
  {
    "purpose": { "coding": [ { "system": "http://terminology.hl7.org/CodeSystem/contactentity-type", "code": "PATINF" } ] },
    "telecom": [ { "system": "phone", "value": "813-555-0100" } ],
    "address": { "line": ["…"], "city": "Lithia", "state": "FL", "postalCode": "33547" }
  }
]
```

> **R4:** Organization and Location have top-level `telecom[]` (and
> Organization `address[]`); the practice phone is one level shallower.

### Code systems you'll use

| Vocabulary | `system` | Used for |
|---|---|---|
| CDT (ADA dental procedure codes) | `http://www.ada.org/cdt` | `serviceType` — D1110 cleaning, D0120 periodic exam, D2391 one-surface composite |
| CPT | `http://www.ama-assn.org/go/cpt` | medical procedures |
| SNOMED CT | `http://snomed.info/sct` | findings, procedures, specialties |
| LOINC | `http://loinc.org` | lab/observation codes |
| ICD-10-CM | `http://hl7.org/fhir/sid/icd-10-cm` | diagnoses |
| NPI | `http://hl7.org/fhir/sid/us-npi` | an *identifier* system — `Practitioner.identifier`, `Organization.identifier` |

**Bindings.** `required` (must be from the set — `Slot.status`,
`Appointment.status`), `extensible`, `preferred`, `example`.
`serviceType` is an *example* binding, which is why CDT is fine there.

---

## 4. References

```json
"subject": { "reference": "Patient/123", "display": "Jane Doe" },
"participant": [
  { "actor": { "reference": "Patient/123" },      "status": "accepted", "required": true },
  { "actor": { "reference": "Practitioner/5" },   "status": "accepted", "required": true },
  { "actor": { "reference": "Location/12" },      "status": "accepted" }
]
```

A **Reference** is `{reference, type, identifier, display}`. `reference` is
usually **relative** (`Patient/123`). It can be **absolute**, or
**logical** (no `reference`, just an `identifier`). `display` is a
convenience and never data.

**References point one way.** `Appointment → Patient`. To get a patient's
appointments you search `Appointment?patient=123` (or, in R5,
`Appointment?subject=123`). No back-references, only searches.

**Contained resources** exist; avoid them.

**`_include` / `_revinclude`** (§6) fetch referenced resources in one call.

**HAPI enforces referential integrity on write by default.** A reference to
a resource that doesn't exist is rejected. Seed in dependency order
(Organization → Location → Practitioner → PractitionerRole → Schedule →
Slot) or in one transaction with `urn:uuid` links (§7.3).

> **R4:** `Appointment.subject` doesn't exist; the patient is only
> discoverable as a `participant.actor`. `participant.required` is a code
> (`required` | `optional` | `information-only`), not a boolean.

---

## 5. The RESTful API (unchanged R4 → R5)

Base URL: `http://localhost:8080/fhir` for HAPI in Compose;
`https://hapi.fhir.org/baseR5` (or `baseR4`) for the public test server.

### 5.1 Interactions

| Interaction | HTTP | Response |
|---|---|---|
| **capabilities** | `GET [base]/metadata` | `CapabilityStatement` — read it first; `fhirVersion` tells you R4 vs R5 |
| **create** | `POST [base]/Patient` (no `id`) | `201`, `Location: …/Patient/123/_history/1`, `ETag: W/"1"` |
| **read** | `GET [base]/Patient/123` | `200` + resource |
| **vread** | `GET [base]/Patient/123/_history/1` | a version |
| **update** | `PUT [base]/Patient/123` (body has `"id"`) | `200`; `versionId` increments |
| **patch** | `PATCH [base]/Patient/123` | JSON Patch / FHIRPath Patch |
| **delete** | `DELETE [base]/Patient/123` | `204`/`200`; HAPI soft-deletes |
| **search** | `GET [base]/Patient?family=Doe` | `Bundle` type `searchset` |
| **history** | `GET [base]/Patient/123/_history` | `Bundle` type `history` |
| **transaction / batch** | `POST [base]` (body: Bundle) | `transaction-response` / `batch-response` |

Headers: `Content-Type: application/fhir+json`, `Accept:
application/fhir+json`, `Prefer: return=representation` (HAPI's default is
`minimal` — empty body, headers only).

### 5.2 Errors: `OperationOutcome`

```json
{ "resourceType": "OperationOutcome",
  "issue": [ { "severity": "error", "code": "processing",
               "diagnostics": "Resource Patient/999 not found, specified in path: Appointment.participant.actor",
               "expression": ["Appointment.participant[0].actor"] } ] }
```

`issue[].code` is from a fixed set (`invalid`, `structure`, `required`,
`value`, `not-found`, `conflict`, `processing`, …). Map these in
`app/fhir/client.py` — step 4 of 5.1.

### 5.3 Conditional operations

- **Conditional create** — `POST` with `If-None-Exist: phone=8135550142`
  (or `ifNoneExist` in a bundle entry). 0 matches → `201`; 1 → `200`,
  nothing written; >1 → `412`. **This makes 5.2's seed idempotent.**
- **Conditional update** — `PUT [base]/Patient?identifier=…`.
- **Conditional delete** — `DELETE [base]/Patient?identifier=…`.

### 5.4 Versioning and optimistic locking

`If-Match: W/"<versionId>"` on a PUT. On mismatch the spec says `412`;
**HAPI returns `409 Conflict`**. Either way: someone else won, re-read. This
is the mechanism behind 5.3's race.

---

## 6. Search (unchanged R4 → R5, one new parameter)

### 6.1 Syntax and result shape

Repeating a parameter **ANDs**; comma inside a value **ORs**.

```json
{ "resourceType": "Bundle", "type": "searchset", "total": 42,
  "link": [ { "relation": "self", "url": "…" }, { "relation": "next", "url": "…" } ],
  "entry": [ { "fullUrl": "…/Slot/77", "resource": { … }, "search": { "mode": "match" } } ] }
```

Follow `link[relation=next]`. `_count=N` sets page size. Included
resources arrive with `search.mode: "include"`.

### 6.2 Parameter types

| Type | Matching | Example |
|---|---|---|
| **string** | case-insensitive **prefix** | `family=Smi` |
| **token** | **exact**, `system\|code` | `status=free`, `identifier=http://hl7.org/fhir/sid/us-npi\|1234567890` |
| **date** | prefix + precision overlap | `date=ge2026-10-06` |
| **reference** | `Type/id` or `id` | `patient=Patient/123` |

Token forms: `system|code`, `|code` (no system), `code` (any system).

### 6.3 Date prefixes

`eq ne gt lt ge le sa eb ap`. Two params make a window:
`start=ge2026-10-06T14:00:00Z&start=lt2026-10-07T00:00:00Z`.

### 6.4 Modifiers

string `:exact`, `:contains` · token `:not`, `:text`, `:in` · reference
`:[Type]` · any `:missing=true|false`.

### 6.5 Crossing references

- `_include=Appointment:patient`, `_include=Appointment:actor`
- `_revinclude=Appointment:patient` on a Patient search
- chaining: `Appointment?patient.family=Smith`, `Slot?schedule.actor=Practitioner/5`
- reverse chaining: `Patient?_has:Appointment:patient:status=booked`

### 6.6 Result control

`_count`, `_sort=start` / `_sort=-start`, `_summary`, `_elements=id,start,end,status`,
`_total=accurate`. Common: `_id`, `_lastUpdated`, `_profile`, `_tag`.

### 6.7 The query that *is* `check_availability`

```
GET /Slot?schedule=Schedule/1&status=free
        &start=ge2026-10-06T14:00:00Z&start=lt2026-10-07T00:00:00Z
        &_sort=start&_count=50
```

Replaces `_candidate_slots()` and `_booked_slot_starts()`. Cost: an HTTP
round-trip inside the confirmation gate (module-review C2). Measure it.

> **R5 adds** `Appointment?subject=Patient/123`. R4 has only
> `patient=` / `actor=`. Everything else above is identical.

---

## 7. Bundle (unchanged R4 → R5)

### 7.1 Types

`document`, `message`, **`transaction`**, `transaction-response`,
**`batch`**, `batch-response`, `history`, **`searchset`**, `collection`.

### 7.2 Transaction vs batch

| | transaction | batch |
|---|---|---|
| Atomic | **yes** | no |
| Intra-bundle references | yes (`urn:uuid:`) | no |
| Use for | booking | bulk reads, independent writes |

### 7.3 The booking transaction (R5 shape)

```json
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {
      "fullUrl": "urn:uuid:8a1f0c8e-2f4e-4b8e-9b2a-1d3f5e7a9c01",
      "resource": { "resourceType": "Patient",
                    "name": [ { "family": "Doe", "given": ["Jane"] } ],
                    "telecom": [ { "system": "phone", "value": "8135550142" } ] },
      "request": { "method": "POST", "url": "Patient", "ifNoneExist": "phone=8135550142" }
    },
    {
      "resource": {
        "resourceType": "Appointment",
        "status": "booked",
        "start": "2026-10-06T14:00:00Z",
        "end":   "2026-10-06T14:30:00Z",
        "minutesDuration": 30,
        "slot": [ { "reference": "Slot/77" } ],
        "subject": { "reference": "urn:uuid:8a1f0c8e-2f4e-4b8e-9b2a-1d3f5e7a9c01" },
        "serviceType": [ { "concept": { "coding": [ { "system": "http://www.ada.org/cdt", "code": "D1110" } ], "text": "cleaning" } } ],
        "participant": [
          { "actor": { "reference": "urn:uuid:8a1f0c8e-2f4e-4b8e-9b2a-1d3f5e7a9c01" }, "status": "accepted", "required": true },
          { "actor": { "reference": "Practitioner/5" }, "status": "accepted", "required": true },
          { "actor": { "reference": "Location/12" },    "status": "accepted" }
        ]
      },
      "request": { "method": "POST", "url": "Appointment" }
    },
    {
      "resource": { "resourceType": "Slot", "id": "77", "schedule": { "reference": "Schedule/1" },
                    "status": "busy", "start": "2026-10-06T14:00:00Z", "end": "2026-10-06T14:30:00Z" },
      "request": { "method": "PUT", "url": "Slot/77", "ifMatch": "W/\"3\"" }
    }
  ]
}
```

> **R4 version of the same bundle:** drop `subject`; write `serviceType`
> as `[ { "coding": [...], "text": "cleaning" } ]` with no `concept`
> wrapper; write `"required": "required"` (a code) instead of `true`, or
> omit it.

- `fullUrl: urn:uuid:…` gives a not-yet-created resource a temporary
  identity; the server rewrites references on commit.
- `ifNoneExist` — conditional create inside a bundle.
- `ifMatch` — optimistic lock inside a bundle.

### 7.4 Processing order — the gotcha

**DELETE → POST → PUT/PATCH → GET/HEAD**, regardless of entry order, then
conditional references are resolved. Your booking is POST then PUT — right
by accident. A GET in the same bundle runs last.

### 7.5 The response

```json
{ "resourceType": "Bundle", "type": "transaction-response",
  "entry": [
    { "response": { "status": "201 Created", "location": "Patient/124/_history/1",     "etag": "W/\"1\"" } },
    { "response": { "status": "201 Created", "location": "Appointment/301/_history/1", "etag": "W/\"1\"" } },
    { "response": { "status": "200 OK",      "location": "Slot/77/_history/4",         "etag": "W/\"4\"" } } ] }
```

On any failure the whole bundle fails with one `OperationOutcome` and
nothing is written — 5.3's "why a transaction and not two REST calls."

---

## 8. The seven resources (+ PractitionerRole), R5

Each: purpose · elements you'll use · search parameters · how it maps here.
`R4:` notes mark what a default HAPI server returns instead.

### 8.1 Patient — the caller

**Elements:** `identifier[]`, `active`, `name[]`, `telecom[]`, `gender`,
`birthDate`, `address[]`, `managingOrganization`, `generalPractitioner[]`,
`communication[]`, `link[]`. (No material change from R4.)

**Search:** `_id`, `identifier`, `name`, `family`, `given`, `birthdate`,
`gender`, **`phone`**, `telecom`, `email`, `address`, `organization`, `active`.

**Here:** `Lead.name` → `name`; `Lead.callback_number` / `Call.caller_number`
→ `telecom[system=phone]`. Lookup: `Patient?phone=8135550142`. Per your 3.5
classification, `birthDate` is not needed for booking — leave it out.

### 8.2 Practitioner — dentist, hygienist

**Elements:** `identifier[]` (NPI), `active`, `name[]`, `telecom[]`,
`address[]`, `gender`, `birthDate`, `deceased[x]` (new), `qualification[]`
(`code`, `issuer`, `period`), `communication[]` (R5: backbone with
`language` + `preferred`).

**Search:** `identifier`, `name`, `family`, `given`, `active`, `phone`,
`email`, `telecom`.

**Here:** not in your schema yet (module-review C4).

### 8.3 PractitionerRole — the join

**Elements:** `practitioner`, `organization`, `location[]`,
`healthcareService[]`, `code[]` (role), `specialty[]`, `period`, `active`,
**`contact[]`** (ExtendedContactDetail), `characteristic[]`,
`communication[]`, **`availability[]`** (Availability), `endpoint[]`.

> **R4:** `telecom[]` instead of `contact`; `availableTime[]`,
> `notAvailable[]`, `availabilityExceptions` instead of `availability`.

**Search:** `practitioner`, `organization`, `location`, `role`,
`specialty`, `active`, `date`.

**Here:** "Dana is a dentist here in these chairs; Maria is a hygienist
here" — the routing fact `check_availability` needs. `availability` is a
*declaration*; `Schedule`/`Slot` is the bookable grid.

### 8.4 Organization — the practice as a legal entity

**Elements:** `identifier[]` (NPI-2, TIN), `active`, `type[]` (`prov`),
`name`, `alias[]`, `description` (new), **`contact[]`**
(ExtendedContactDetail — phone *and* address live here), `partOf`,
`endpoint[]`, `qualification[]` (new).

> **R4:** top-level `telecom[]` and `address[]`; no `description`,
> `qualification`.

**Search:** `identifier`, `name`, `address`, `partof`, `type`, `active`,
`phonetic`.

**Here:** `Practice.name` → `name`; `Practice.phone` →
`contact[0].telecom[0]` (R5) / `telecom[0]` (R4). **No timezone element**
in either version (§9).

### 8.5 Location — the site, and each chair

**Elements:** `identifier[]`, `status` (active | suspended | inactive),
`operationalStatus`, `name`, `alias[]`, `description`, `mode` (instance |
kind), `type[]`, **`contact[]`** (ExtendedContactDetail), `address` (0..1),
**`form`** (was `physicalType`), `position`, `managingOrganization`,
**`partOf`**, `characteristic[]` (new), **`hoursOfOperation[]`**
(Availability), `virtualService[]` (new), `endpoint[]`.

> **R4:** `telecom[]` not `contact`; `physicalType` not `form`;
> `hoursOfOperation` is the flat backbone; `availabilityExceptions` exists.

**Search:** `identifier`, `name`, `address`, `address-city`, `organization`,
`partof`, `status`, `type`, `near`, `characteristic` (R5).

**Here:** `Practice.business_hours` → `hoursOfOperation[0].availableTime[]`
(R5) or `hoursOfOperation[]` entries (R4); same `mon…sun` codes. A chair /
operatory is a Location with `form` = room and `partOf` → the site; Open
Dental's `Op` is exactly this.

### 8.6 Schedule — an actor's availability container

**Elements:** `identifier[]`, `active`, `serviceCategory[]`,
`serviceType[]` (CodeableReference), `specialty[]`, **`name`** (new),
**`actor[]` (1..*)** (→ Patient | Practitioner | PractitionerRole |
CareTeam | RelatedPerson | Device | HealthcareService | Location),
`planningHorizon`, `comment` (markdown).

**Search:** `actor`, `active`, `date`, `identifier`, `name` (R5),
`service-category`, `service-type`, `specialty`.

**Here:** holds no times — parent of Slots. One per provider *or* per
chair (C4). `Schedule?actor=Practitioner/5`.

### 8.7 Slot — one bookable unit of time

**Elements:** `identifier[]`, `serviceCategory[]`, `serviceType[]`
(CodeableReference), `specialty[]`, `appointmentType[]` (R5: 0..*),
**`schedule` (1..1)**, **`status` (1..1)**: `free` | `busy` |
`busy-unavailable` | `busy-tentative` | `entered-in-error`,
**`start` (1..1, instant)**, **`end` (1..1, instant)**, `overbooked`,
`comment`.

> **R4:** `serviceType` is CodeableConcept; `appointmentType` is 0..1.

**Search:** `schedule`, `status`, `start`, `identifier`, `service-category`,
`service-type`, `specialty`, `appointment-type`. No `end` parameter —
window on `start`.

**Here:** the query in §6.7. `end − start` is the duration your schema
lacks (C3). `status=busy` is the second write in your transaction.

### 8.8 Appointment — the booking

**Elements (R5, verified):** `identifier[]`, **`status` (1..1)**:
`proposed` | `pending` | `booked` | `arrived` | `fulfilled` | `cancelled`
| `noshow` | `entered-in-error` | `checked-in` | `waitlist`;
**`cancellationReason`** (two *l*'s), `class[]`, `serviceCategory[]`,
**`serviceType[]`** (CodeableReference), `specialty[]`, `appointmentType`,
**`reason[]`** (CodeableReference), `priority`, `description`,
**`replaces[]`**, `virtualService[]`, `supportingInformation[]`,
**`previousAppointment`**, **`originatingAppointment`**, `start`
(instant), `end` (instant), `minutesDuration`, `requestedPeriod[]`,
`slot[]`, `account[]`, `created`, **`cancellationDate`**, **`note[]`**
(Annotation), `patientInstruction[]` (CodeableReference), `basedOn[]`,
**`subject`** (→ Patient | Group), **`participant[]` (1..*)**: `type[]`,
`period`, `actor` (→ Patient | Group | Practitioner | PractitionerRole |
CareTeam | RelatedPerson | Device | HealthcareService | Location),
**`required` (boolean)**, **`status` (1..1)**: `accepted` | `declined` |
`tentative` | `needs-action`; `recurrenceId`, `occurrenceChanged`,
`recurrenceTemplate` (with its own `timezone`).

> **R4:** `cancelationReason` (one *l*); no `subject`, `class`,
> `replaces`, `previousAppointment`, `originatingAppointment`,
> `cancellationDate`, `virtualService`, `account`, recurrence;
> `reasonCode[]` + `reasonReference[]` instead of `reason`; `comment`
> (string) instead of `note`; `serviceType` is CodeableConcept;
> `participant.required` is a code.

**Invariants you'll hit (R5):** start and end both present or both absent;
only `proposed` or `cancelled` may omit start/end (so a `booked`
Appointment **must** carry both); start ≤ end; `cancellationReason` and
`cancellationDate` only on `cancelled` or `noshow`; a participant must
have `type` or `actor`; `originatingAppointment` and `recurrenceTemplate`
are mutually exclusive.

**Search:** `actor`, `patient`, **`subject`** (R5), `practitioner`,
`location`, `date`, `status`, `identifier`, `slot`, `service-type`,
`specialty`, `appointment-type`, `part-status`, `based-on`, `reason-code`,
`reason-reference`, `requested-period` (R5).

**Here:** your `Appointment` row. `serviceType` is the column you folded
into `Lead.notes` (C3). Status mapping in §9. Dr. Dana's day:
`Appointment?actor=Practitioner/5&date=ge2026-10-06&date=lt2026-10-07`.

### 8.9 How they fit

```
Organization (the practice)  ── contact (phone, address)
 └── Location (the site)  ── hoursOfOperation (Availability)
      ├── Location (Chair 1, partOf site, form=room)
      └── Location (Chair 2, partOf site, form=room)
PractitionerRole: Practitioner ↔ Organization ↔ Location[], specialty, availability
Schedule (name, actor = Practitioner/5 or Location/12, planningHorizon)
 └── Slot, Slot, Slot …  (status free|busy, start/end)
Appointment
 ├── subject → Patient/123          (R5)
 ├── slot → Slot/77
 ├── previousAppointment → Appointment/299   (R5, on reschedule)
 └── participant[]: actor Patient/123, Practitioner/5, Location/12
```

`Slot` says *when* something can be booked. `Appointment` says *what was*
booked and *with whom*. In R5 the patient is named twice — `subject` for
"whose appointment," `participant` for "who attends" — and the participant
list remains the booking's identity. That's the 5.1 self-check answer.

---

## 9. Mapping notes for this project

| Yours | FHIR R5 | Note |
|---|---|---|
| `Practice` | `Organization` + `Location` (site) | two resources |
| `Practice.phone` | `Organization.contact[].telecom[]` | **R4:** `Organization.telecom[]` |
| `Practice.business_hours` | `Location.hoursOfOperation[].availableTime[]` | **R4:** flat `hoursOfOperation[]`; same day codes either way |
| `Practice.timezone` | **nothing** | no timezone on Organization or Location in R4 *or* R5. Only `Appointment.recurrenceTemplate.timezone` exists, for recurrence. Options: an extension, or derive from `Location.address` at seed time. Decide in `fhir-mapping.md`. |
| (missing) dentist / hygienist | `Practitioner` + `PractitionerRole` | C4 |
| (missing) chair / operatory | `Location` with `partOf`, `form=room` | Open Dental `Op` |
| `Call.caller_number`, `Lead.name/callback_number` | `Patient.telecom`, `Patient.name` | `Patient?phone=` |
| `_candidate_slots` + `_booked_slot_starts` | `Slot` search (§6.7) | one query |
| `SLOT_MINUTES` | `Slot.end − Slot.start`; `Appointment.minutesDuration` | duration becomes data (C3) |
| `Lead.notes: "Service: cleaning"` | `Appointment.serviceType[].concept` (CDT) | **R4:** `serviceType[]` directly. First-class either way (C3) |
| `Appointment.requested_slot` | `start` + `end` + `slot[]` | both required when `booked` |
| `AppointmentStatus.pending` | `pending` | |
| `AppointmentStatus.confirmed` | `booked` | |
| `AppointmentStatus.cancelled` | `cancelled` + `cancellationReason` + `cancellationDate` | **R4:** `cancelationReason`, no date field |
| `AppointmentStatus.rescheduled` | old → `cancelled`; new → `booked` with **`previousAppointment` → old** | R5 gives your close-old-open-new pattern a first-class link. **R4:** no such status and no link — the chain is only recoverable by convention (identifier, or `basedOn`). |
| `Appointment.confirmation` | `participant.status=accepted` or an `identifier` | |
| the caller, as a field | `Appointment.subject` | **R4:** participants only |

---

## 10. HAPI FHIR specifics

- Image: `hapiproject/hapi`; source and Compose examples in
  `hapifhir/hapi-fhir-jpaserver-starter`. Version is an environment
  variable: `hapi.fhir.fhir_version=R5` (or `R4`, the default).
- **Switch versions only on a fresh database.** A reported startup failure
  when flipping R4 → R5 on an existing volume is a schema-mismatch, not a
  bug you can work around — `docker compose down -v` first.
- Base URL in Compose: `http://hapi:8080/fhir` from another container,
  `http://localhost:8080/fhir` from the host. Web UI at `/`.
- Default database is embedded H2; give it Postgres for anything you want
  to keep (`spring.datasource.*` in the starter README).
- **Referential integrity on write is on by default** — seed in dependency
  order or in one transaction.
- Version conflict on `If-Match` → **409**, not 412.
- Transactions, conditional create/update, `_include`/`_revinclude`,
  chaining, `_has` all supported in both versions. `/metadata` →
  `fhirVersion` tells you which you're actually running; `rest.resource[].searchParam`
  tells you what's indexed.
- `Prefer: return=representation` to get bodies back on writes.
- First start is slow; healthcheck on `/metadata` before seeding.
- Public shared test servers: `https://hapi.fhir.org/baseR5` and
  `https://hapi.fhir.org/baseR4` — never put real data there. Compare the
  same `Appointment` on both to see §0 in practice.

---

## 11. Profiles and auth — enough to not be wrong

**Profiles / Implementation Guides** constrain base FHIR. **US Core** is
the US national profile set and what "ONC-certified FHIR API" means. It is
**R4-based** — there is no US Core for R5 — and it profiles Patient,
Practitioner, PractitionerRole, Organization, Location, **but not
Schedule, Slot or Appointment**. Scheduling has no national profile in
either version, which is why every vendor's scheduling API differs and
"FHIR-compliant" scheduling still means integration work.

**SMART on FHIR** is the OAuth2 layer and is version-independent:
- *EHR launch* — a clinician clicks your app inside the EHR.
- *Standalone launch* — a user opens your app and logs in to the EHR.
- **Backend Services** — no user. Your server authenticates with a signed
  JWT client assertion (RFC 7523), gets a token with `system/` scopes
  (`system/Appointment.rs`, `system/Slot.rs` in SMART v2 syntax). **This
  is your agent's flow** — a service answering a phone at 2 a.m. 5.5's
  diagram should be this one (module-review C1).

**Bulk Data (`$export`)** — asynchronous NDJSON export. Awareness only.

---

## 12. Errors you will see

| Status | Typical cause | What to do |
|---|---|---|
| `400` | malformed JSON, unknown element (**including an R5 element sent to an R4 server** — `subject`, `cancellationReason`, `form`, `contact` on Organization), **reference to a missing resource** (HAPI) | read `OperationOutcome.issue[].diagnostics` |
| `404` | unknown id or resource type | |
| `405` | interaction not supported (`/metadata`) | |
| `409` | **`If-Match` mismatch on HAPI**; duplicate on conditional create | re-read, re-decide — the race path |
| `410` | deleted | |
| `412` | spec's `If-Match` code; conditional create matched >1 | |
| `422` | invariant violation (e.g. `booked` without `start`/`end`), profile failure | |
| `200` with `total: 0` | search matched nothing — **not** an error | your `ToolError` decision |

An empty search is `200` with an empty `entry`, never `404`. Map it
deliberately — step 4 of 5.1.

---

## 13. Glossary

- **Resource** — a typed FHIR document.
- **Logical id / Identifier** — server row key / business id with a `system`.
- **Reference** — a one-way pointer.
- **CodeableConcept / Coding** — a coded value with its vocabulary.
- **CodeableReference** (R5) — a concept and/or a reference, in one element.
- **Availability** (R5) — working hours + exceptions, one datatype.
- **ExtendedContactDetail** (R5) — a purposed contact with telecoms and address.
- **Annotation** — a text note with author and time.
- **Bundle** — a container; `transaction` is atomic.
- **Profile / IG** — constraints on base FHIR (US Core, Epic's).
- **CapabilityStatement** — `/metadata`.
- **OperationOutcome** — the error resource.
- **SMART on FHIR** — the OAuth2 profile; Backend Services is the no-user flow.
- **CDT** — ADA's dental procedure codes (D-codes).
- **Interface engine** — Mirth/Rhapsody/Cloverleaf; FHIR ↔ HL7 v2 at a hospital (5.4).

---

## 14. Sources

R5 spec (read *Scope and Usage*, *Boundaries and Relationships*, the
element table, and *Search Parameters* on each resource page; the **R4
Diff** tab on each page is the authoritative change list):

- Resource basics — https://hl7.org/fhir/R5/resource.html
- Data types — https://hl7.org/fhir/R5/datatypes.html (CodeableReference, Availability, ExtendedContactDetail are on this page)
- References — https://hl7.org/fhir/R5/references.html
- RESTful API — https://hl7.org/fhir/R5/http.html (transaction: `#transaction`)
- Search — https://hl7.org/fhir/R5/search.html
- Bundle — https://hl7.org/fhir/R5/bundle.html
- Patient — https://hl7.org/fhir/R5/patient.html
- Practitioner — https://hl7.org/fhir/R5/practitioner.html
- PractitionerRole — https://hl7.org/fhir/R5/practitionerrole.html
- Organization — https://hl7.org/fhir/R5/organization.html
- Location — https://hl7.org/fhir/R5/location.html
- Schedule — https://hl7.org/fhir/R5/schedule.html
- Slot — https://hl7.org/fhir/R5/slot.html
- Appointment — https://hl7.org/fhir/R5/appointment.html
- OperationOutcome — https://hl7.org/fhir/R5/operationoutcome.html

R4 equivalents (what your server, Epic and US Core actually run) — replace
`/R5/` with `/R4/` in any URL above.

- US Core (R4) — https://hl7.org/fhir/us/core/
- SMART App Launch (incl. Backend Services) — https://hl7.org/fhir/smart-app-launch/
- HAPI FHIR docs — https://hapifhir.io/hapi-fhir/docs/
- HAPI JPA starter — https://github.com/hapifhir/hapi-fhir-jpaserver-starter
- HAPI public test servers — https://hapi.fhir.org/baseR5 · https://hapi.fhir.org/baseR4
