# HL7 v2 — a longread for lesson 5.4

Reading material for 5.4's "Read (60 min)", written 2026-10-02. Every
example message in this file was built and parsed with `hl7apy` (strict
validation, v2.5.1) and `python-hl7` before it went in here — what they
said is quoted where it matters.

This is **input**, not the 5.4 deliverables. Three things stay yours:
the table of six message types, the SIU^S12 generated from *your* 5.3
booking, and the paragraph on how your agent plugs into a v2-only
hospital. §11 gives you the facts that paragraph has to rest on, not the
paragraph.

**Plan for the 60 minutes:** §1–§2 (15 min) → §3–§4 (15 min) → §5 with
the three annotated messages (20 min) → skim §6–§9 (10 min). §10–§14 are
for the Do half and for the interview.

---

## 1. Why v2 still matters, in one page

HL7 version 2 is a messaging standard from the late 1980s. It is not
elegant, not RESTful, not JSON — and it moves the overwhelming majority of
clinical data inside US hospitals today. Every registration, every
admission, every lab result, every scheduled appointment, every
transcribed note travels between a hospital's systems as a v2 message.

The reason is simple: by the time FHIR appeared (2014) and became
regulation-backed (around 2020), every hospital had already spent two
decades building hundreds of v2 interfaces between its lab system, its
radiology system, its pharmacy, its billing and its EHR. They work. Nobody
rips out working plumbing to install nicer pipes.

So the mental model is:

- **v2 is the plumbing.** Inside the hospital, system to system, events
  pushed in real time, mostly invisible.
- **FHIR is the front door.** For apps, outside parties, patients and
  regulators — pull on demand, over REST.

They coexist. An FDE who walks into a hospital and only speaks FHIR will
find that half of what they need — especially "tell me *when* something
happens" — still arrives as v2.

**The core idea of v2 in one sentence:** *something happens in the real
world (a patient is registered, an appointment is booked, a result is
ready), and the system where it happened sends a message announcing it to
everyone who needs to know.* That's why v2 is described as
**event-driven** and **push-based**: you don't ask v2 for data, data comes
to you.

---

## 2. Anatomy of a message

Here is a real-shaped registration message. Read it once without
understanding, then we'll take it apart.

```
MSH|^~\&|REG|SUNHOSP|LAB|SUNHOSP|20261006093000||ADT^A04^ADT_A01|MSG00017|P|2.5.1
EVN|A04|20261006093000
PID|1||100245^^^SUNHOSP^MR||Doe^Jane^M||19870314|F|||14 Main St^^Tampa^FL^33601||^PRN^PH^^1^813^5550142
PV1|1|O|CARD^CLINIC1^^SUNHOSP||||1234567890^Ruiz^Ana^^^Dr.
```

### 2.1 The hierarchy

| Level | Separator | What it is | Example |
|---|---|---|---|
| **Message** | — | one event | the whole block above |
| **Segment** | carriage return `\r` | one line, a logical group | `PID|…` (patient identification) |
| **Field** | `\|` pipe | one data element | `Doe^Jane^M` (PID-5, the name) |
| **Repetition** | `~` tilde | the same field repeated | two phone numbers in one field |
| **Component** | `^` caret | a part of a field | `Doe` (family), `Jane` (given) |
| **Subcomponent** | `&` ampersand | a part of a component | rare; e.g. inside an assigning authority |

Every segment starts with a **three-letter code** that says what it
is: MSH, PID, PV1, OBX. The code is the only "label" in the whole format —
fields have no names, only positions. That's the central fact of v2:
**meaning comes from position.** PID-5 is the patient name because it is
the fifth field of PID, and every v2 implementation agrees on that.

### 2.2 Addressing: how people refer to a value

`PID-5` = field 5 of PID → `Doe^Jane^M`
`PID-5.1` = its first component → `Doe`
`PID-5.2` = second component → `Jane`
`PID-3.4` = fourth component of field 3 → `SUNHOSP` (who issued the MRN)

You'll see this notation in every interface specification, in every
interface engine, and in every conversation with a hospital integration
analyst. Learn to say "PID-3" and "OBX-5" fluently — it's the vocabulary.

### 2.3 MSH-1 and MSH-2 — the message describes its own separators

The first segment is always **MSH** (Message Header), and its first two
fields are special:

- **MSH-1** is the field separator itself — the `|` right after `MSH`.
- **MSH-2** is the four **encoding characters**: `^~\&` — component,
  repetition, escape, subcomponent, in that order. (v2.7+ adds a fifth,
  `#`, the truncation character. You'll rarely see it.)

This has two consequences:

1. **Field numbering in MSH is off by one compared to the text.** Because
   the first `|` *is* MSH-1, the sending application `REG` is MSH-3, not
   MSH-2. Every other segment starts counting at the first field after the
   segment code. Parsers handle this — `python-hl7` returned `'|'` for
   `MSH[1]` and `'^~\&'` for `MSH[2]` on the message above — but when you
   count pipes by eye in MSH, you'll be off by one until it's automatic.
2. **In theory** a system could choose different separators. In practice,
   `|^~\&` is universal. If you see something else, ask why.

### 2.4 Escape sequences

If a value contains a separator character, it's escaped with `\`:

| Sequence | Means |
|---|---|
| `\F\` | `\|` field separator |
| `\S\` | `^` component separator |
| `\T\` | `&` subcomponent separator |
| `\R\` | `~` repetition separator |
| `\E\` | `\` escape character itself |
| `\.br\` | line break (in text fields) |
| `\Xhh\` | hex data |

So a practice name "Smith & Sons" becomes `Smith \T\ Sons`. A free-text
note with a line break uses `\.br\`. Parsers decode these for you; when
you *generate* messages, you must encode them or you'll silently corrupt
the structure.

### 2.5 Empty is not null

This is a subtle rule that bites integrators:

- **Empty field** (`||`) means "not sent / no information" — the receiver
  should **leave its current value alone**.
- **Two double quotes** (`|""|`) means "**delete** this value" — the
  receiver should clear what it has.

An ADT^A08 (update patient) with an empty phone field means "phone
unchanged." With `""` it means "patient no longer has a phone." Getting
this wrong wipes data in downstream systems.

### 2.6 Trailing fields can be dropped

`PV1` has over fifty defined fields. In the message above it stops after
PV1-7, because everything after that is empty. Senders routinely drop
trailing empty fields and receivers must accept that. Likewise, receivers
are expected to **ignore** fields and segments they don't understand —
this is how v2 stays backward-compatible across versions.

### 2.7 Segment terminator: carriage return, not newline

Segments are separated by `\r` (carriage return, 0x0D) — **not** `\n`.
This is the most common first-day bug. When I fed the ADT message above to
`python-hl7` with `\n` line endings instead of `\r`, it **did not
raise an error** — it silently returned one segment, `MSH`, with the whole
message stuffed inside it. A silent wrong answer, not a crash. Normalize
line endings before you parse.

---

## 3. MSH — the envelope

Every message starts with MSH. You read MSH first, every time, because it
tells you what everything after it means.

| Field | Name | In the example | Why you care |
|---|---|---|---|
| MSH-1 | Field separator | `\|` | |
| MSH-2 | Encoding characters | `^~\&` | |
| MSH-3 | Sending application | `REG` | who sent it (registration system) |
| MSH-4 | Sending facility | `SUNHOSP` | which hospital / site |
| MSH-5 | Receiving application | `LAB` | who it's for |
| MSH-6 | Receiving facility | `SUNHOSP` | |
| MSH-7 | Date/time of message | `20261006093000` | `YYYYMMDDHHMMSS`, optional `±ZZZZ` offset — often **local time without an offset**, which is a timezone trap |
| MSH-8 | Security | (empty) | almost always empty |
| **MSH-9** | **Message type** | `ADT^A04^ADT_A01` | **the single most important field** — type ^ trigger event ^ structure |
| **MSH-10** | **Message control ID** | `MSG00017` | unique per message; echoed back in the ACK so the sender knows *which* message was acknowledged |
| MSH-11 | Processing ID | `P` | `P` production, `T` training/test, `D` debugging. A test message sent with `P` to production is a real incident |
| **MSH-12** | **Version** | `2.5.1` | which edition of the standard the sender claims |
| MSH-15 / 16 | Accept / application ACK type | (empty) | control acknowledgment mode (§7.3) |

**MSH-9 has three components:** the message type (`ADT`), the trigger
event (`A04`), and the message structure (`ADT_A01`). Note that an A04 uses
the `ADT_A01` structure — several trigger events share one structure.
That's normal: the *event* tells you what happened, the *structure* tells
you which segments to expect.

**MSH-7 time zones:** v2 timestamps allow an offset (`20261006093000-0400`)
but many systems send local time without one. Your own `localize_slot()`
design — "naive means practice-local" — is exactly the assumption v2
interfaces make implicitly. In an interface specification, the time zone
convention is something you confirm in writing.

---

## 4. The segments you'll meet

### 4.1 Patient and visit

**PID — Patient Identification.** Who the message is about.

| Field | Content | Note |
|---|---|---|
| PID-3 | Patient identifier list | `100245^^^SUNHOSP^MR` — ID ^ check digit ^ scheme ^ **assigning authority** ^ **identifier type** (MR = medical record number). Repeats with `~` for multiple IDs |
| PID-5 | Patient name | `Doe^Jane^M` — family ^ given ^ middle |
| PID-7 | Date of birth | `19870314` |
| PID-8 | Administrative sex | `F` |
| PID-11 | Address | street ^ other ^ city ^ state ^ zip |
| PID-13 | Home phone | `^PRN^PH^^1^813^5550142` — in v2.5 the number is in components (country, area, local), not the first component |
| PID-18 | Patient account number | billing account, distinct from MRN |

PID-3 is the one to understand deeply. A patient has **many identifiers**
across systems — the hospital MRN, the lab's own number, an insurance
member ID — and the assigning authority (PID-3.4) says whose number it is.
The same person can be `100245` in one system and `A-77812` in another;
matching them is the job of a Master Patient Index, and getting it wrong is
how one patient's results land in another patient's chart.

**PV1 — Patient Visit.** The encounter.

| Field | Content |
|---|---|
| PV1-2 | Patient class: `I` inpatient, `O` outpatient, `E` emergency |
| PV1-3 | Assigned location: point of care ^ room ^ bed ^ facility |
| PV1-7 | Attending doctor: ID ^ family ^ given ^ middle ^ suffix ^ prefix |
| PV1-19 | Visit number (the encounter ID) |
| PV1-44 | Admit date/time |

**EVN — Event Type.** When the triggering event happened (EVN-2), which
can differ from when the message was sent (MSH-7).

### 4.2 Scheduling — your segments

**SCH — Scheduling Activity Information.** The appointment itself.

| Field | Content | Note |
|---|---|---|
| SCH-1 | Placer appointment ID | the ID given by the system that *requested* the booking |
| **SCH-2** | **Filler appointment ID** | the ID given by the system that *owns the schedule* — this is the key you store |
| SCH-6 | Event reason | **required** in 2.5.1 |
| SCH-7 | Appointment reason | coded, table HL70276: ROUTINE, WALKIN, CHECKUP, FOLLOWUP, EMERGENCY |
| SCH-8 | Appointment type | table HL70277: Normal, Tentative, Complete |
| SCH-9 / SCH-10 | Duration / units | `30` / `MIN` |
| SCH-11 | Timing (TQ) | start and end packed into components — **deprecated in 2.5**, replaced by the TQ1 segment, but still widely sent |
| SCH-16 | Filler contact person | **required** in 2.5.1 |
| SCH-20 | Entered-by person | **required** in 2.5.1 |
| **SCH-25** | **Filler status code** | `Booked`, `Cancelled`, `Complete`, `Noshow`, `Pending`, … — the appointment's state |

**Placer and filler** are v2's two roles, and they explain the whole
scheduling model. The **placer** asks for something; the **filler** fulfils
it and owns the result. For scheduling: the system that owns the calendar
is the filler. Its ID (SCH-2) is authoritative. Keep this distinction — it
comes back in §11.

**TQ1 — Timing/Quantity.** The modern home for start/end: TQ1-7 start,
TQ1-8 end.

**RGS — Resource Group.** A one-line header that opens the list of
resources for the appointment. It looks like nothing (`RGS|1|A`), and it
is **mandatory** before any AIS/AIG/AIL/AIP. I verified this:
`hl7apy` strict validation of an SIU^S12 without RGS fails with
*"Missing required child SIU_S12_RESOURCES.RGS"*. Tolerant parsing
silently accepts it — another case where the lenient path hides a defect
the receiving system may reject.

**The four resource segments** — what the appointment needs:

| Segment | Resource | Key field | Your equivalent |
|---|---|---|---|
| **AIS** | Service — *what* is being done | AIS-3: service code (CPT, or a local code) | `service` (cleaning, filling) |
| **AIG** | General resource — equipment, other | AIG-3 | rare in dental |
| **AIL** | Location — *where* | AIL-3: location (point of care ^ room ^ … ^ facility) | the chair / operatory |
| **AIP** | Personnel — *who* | AIP-3: provider; AIP-4: role (attending, …) | the dentist or hygienist |

Each carries its own start (AIS-4; AIL-6; AIP-6), duration and units, and a
**segment action code** in field 2: `A` add, `D` delete, `U` update. That
lets a reschedule say "same appointment, different chair."

Compare with FHIR: AIL and AIP are exactly `Appointment.participant.actor`
→ Location and → Practitioner. Same concept, thirty years earlier.

### 4.3 Orders and results

**ORC — Common Order.** ORC-1 order control code (`NW` new, `CA` cancel,
`RE` observations to follow), ORC-2 placer order number, ORC-3 filler order
number.

**OBR — Observation Request.** The *test* that was ordered. OBR-4
universal service ID (usually a LOINC code), OBR-7 observation date/time.

**OBX — Observation/Result.** The *value*. The workhorse segment of v2 —
lab values, vital signs, device measurements, even whole documents.

| Field | Content |
|---|---|
| OBX-2 | Value type: `NM` numeric, `ST` string, `TX` text, `CWE`/`CE` coded, `ED` encapsulated data (e.g. a base64 PDF) |
| OBX-3 | Observation identifier — what was measured (LOINC) |
| OBX-5 | **The value** |
| OBX-6 | Units |
| OBX-7 | Reference range |
| OBX-8 | Abnormal flag: `H` high, `L` low, `A` abnormal, `N` normal, `HH`/`LL` critical |
| OBX-11 | Result status: `F` final, `P` preliminary, `C` corrected |
| OBX-14 | Date/time of the observation |

OBX-2 tells you how to read OBX-5. A numeric value, a coded value and a
200-KB embedded PDF all live in OBX-5 — the type field is how you know
which.

**NTE — Notes and Comments.** Free text, attached to the segment before
it.

### 4.4 Documents and acknowledgments

**TXA — Transcription Document Header.** Document type, status,
authentication, unique document ID — used in MDM messages, with the
content itself in OBX segments.

**MSA — Message Acknowledgment.** MSA-1 the acknowledgment code, MSA-2
the control ID (MSH-10) of the message being acknowledged.

**ERR — Error.** What went wrong, where, how severe.

### 4.5 Z-segments

Any segment whose code starts with **Z** (`ZPI`, `ZDN`, `ZAP`) is a local,
site-specific extension. v2 explicitly allows them. They're the v2
equivalent of FHIR extensions — and they're everywhere. A hospital's
interface specification will list its Z-segments, and they often carry the
data you actually need.

---

## 5. Message types and trigger events

MSH-9 is `TYPE^EVENT`. The type is the family; the event is what happened.

### 5.1 ADT — Admit, Discharge, Transfer

The highest-volume family in any hospital. Every time a patient is
registered, admitted, moved, discharged, or has their demographics edited,
an ADT message goes out to every system that keeps a patient list.

| Event | Meaning |
|---|---|
| **A01** | Admit / visit notification — inpatient admission |
| A02 | Transfer — patient moved |
| A03 | Discharge / end visit |
| **A04** | Register a patient — outpatient or ED visit begins |
| **A08** | Update patient information — the **most frequent** message in most hospitals |
| A11 / A12 / A13 | Cancel admit / transfer / discharge |
| A28 / A31 | Add / update person information (no visit) |
| A40 | Merge patient — two records are the same person |

Typical structure: `MSH, EVN, PID, [PD1], [NK1…], PV1, [PV2], [OBX…],
[AL1…], [DG1…], [IN1…]` — the patient, the visit, and optionally next of
kin, allergies, diagnoses and insurance.

**A40 deserves a sentence.** When two records turn out to be the same
person, A40 tells every downstream system to merge them. Systems that
ignore merges end up with split histories — half a patient's results under
each MRN. An interface that doesn't handle A40 is a patient-safety issue,
not a cosmetic one.

**Annotated:**

```
MSH|^~\&|REG|SUNHOSP|LAB|SUNHOSP|20261006093000||ADT^A04^ADT_A01|MSG00017|P|2.5.1
```
Registration system at Sun Hospital tells the lab system, at 09:30 on
6 Oct 2026: *an outpatient was registered* (A04, A01 structure). Control
ID MSG00017. Production. Version 2.5.1.

```
EVN|A04|20261006093000
```
The event happened at the same moment.

```
PID|1||100245^^^SUNHOSP^MR||Doe^Jane^M||19870314|F|||14 Main St^^Tampa^FL^33601||^PRN^PH^^1^813^5550142
```
Patient: MRN 100245 issued by SUNHOSP. Jane M. Doe, born 14 Mar 1987,
female, Tampa address, primary phone (813) 555-0142.

```
PV1|1|O|CARD^CLINIC1^^SUNHOSP||||1234567890^Ruiz^Ana^^^Dr.
```
Outpatient visit in the cardiology clinic, attending Dr. Ana Ruiz
(NPI-style ID 1234567890).

### 5.2 SIU — Scheduling Information Unsolicited (yours)

**"Unsolicited"** is the key word: the scheduling system *announces*
changes to its calendar without being asked. SIU messages are
**notifications from the filler** — the system that owns the schedule —
telling everyone else what happened.

| Event | Meaning |
|---|---|
| **S12** | Notification of **new** appointment booking |
| **S13** | Notification of appointment **rescheduling** |
| S14 | Notification of appointment **modification** (same time, other details changed) |
| **S15** | Notification of appointment **cancellation** |
| S16 | Discontinuation (remaining occurrences of a recurring series) |
| S17 | Deletion (removed as if it never existed — rare, usually for errors) |
| S26 | Patient **did not show** up |

Structure: `MSH, SCH, [TQ1], [NTE], [PID, PV1 …], RGS, [AIS], [AIG],
[AIL], [AIP]` — with the patient block optional and the resource block
repeating per resource group.

**S13 vs S14** is the distinction people get wrong: S13 means the *time*
moved; S14 means something *else* changed (provider notes, reason, a
resource) while the time stayed.

**Annotated** — a pacemaker follow-up in an EP clinic. This version passes
`hl7apy` strict validation for v2.5.1:

```
MSH|^~\&|SCHED|SUNHOSP|EPREMOTE|ACME|20261006141502||SIU^S12^SIU_S12|SIU55021|P|2.5.1
```
The hospital's scheduling system tells an outside EP remote-monitoring
vendor: *a new appointment was booked.*

```
SCH||F33019^SCHED||||NEW^New booking|FOLLOWUP^Follow-up^HL70276|Normal^Routine^HL70277|30|MIN||||||9999^Front^Desk||||9999^Front^Desk|||||Booked
```
No placer ID (SCH-1 empty — booked directly in the scheduling system).
Filler appointment ID F33019 (SCH-2) — **the ID that matters**. Reason:
follow-up (SCH-7). Type: normal (SCH-8). 30 minutes (SCH-9/10). Filler
contact and entered-by person (SCH-16, SCH-20 — required). Status:
**Booked** (SCH-25).

Count the pipes between `MIN` and `9999` once by hand. That exercise is
why every integrator eventually stops counting and lets a parser do it.

```
TQ1|1||||||20261013090000|20261013093000
```
13 Oct 2026, 09:00–09:30. No offset — local time by convention.

```
PID|1||100245^^^SUNHOSP^MR||Doe^Jane^M||19870314|F
PV1|1|O|EPCLINIC^^^SUNHOSP
```
Same patient; outpatient; EP clinic.

```
RGS|1|A
AIS|1|A|93288^Interrogation device evaluation, in person, pacemaker^CPT4|20261013090000|||30|MIN
AIL|1|A|EPCLINIC^ROOM4^^SUNHOSP|||20261013090000|||30|MIN
AIP|1|A|1234567890^Ruiz^Ana^^^Dr.|ATT^Attending^HL70443||20261013090000|||30|MIN
```
Resource group 1, added. *What:* CPT 93288, in-person pacemaker
interrogation. *Where:* EP clinic, room 4. *Who:* Dr. Ruiz, attending. All
30 minutes from 09:00.

When I first built this message without SCH-6, SCH-16 and SCH-20, `hl7apy`
in **tolerant** mode parsed it happily; **strict** mode rejected it three
times, once per field. Real interfaces live somewhere in between — which
fields a receiver actually requires is in its interface specification, not
in the standard.

**SRM / SRR — the other half of scheduling.** v2 also defines **SRM**
(Schedule Request Message) for a *placer* to ask a filler to book, modify
or cancel (`SRM^S01` request new booking, `S03` modification, `S04`
cancellation …), answered by **SRR**. In practice, **SRM is rarely
implemented** — most hospital scheduling systems only *emit* SIU and accept
bookings through their own UI or a vendor API. That gap is exactly why
third-party booking tools struggle with v2-only hospitals, and it shapes
§11.

### 5.3 ORU — Observation Result Unsolicited

Lab, radiology, cardiology and device results pushed to the EHR when
they're ready.

Structure: `MSH, PID, [PV1], {ORC, OBR, {OBX, [NTE]}}` — per order, a
request (OBR) and its result lines (OBX).

**Annotated** — an INR result (relevant to you twice: anticoagulated AF
patients, and a dentist planning an extraction):

```
MSH|^~\&|LIS|SUNHOSP|EHR|SUNHOSP|20261006113000||ORU^R01^ORU_R01|LAB88231|P|2.5.1
PID|1||100245^^^SUNHOSP^MR||Doe^Jane^M||19870314|F
ORC|RE|ORD4471|LAB88231
OBR|1|ORD4471|LAB88231|6301-6^INR in Platelet poor plasma by Coagulation assay^LN|||20261006100500
OBX|1|NM|6301-6^INR in Platelet poor plasma by Coagulation assay^LN||2.8||0.8-1.1|H|||F|||20261006100500
```

Lab system → EHR. Observations to follow (ORC-1 `RE`) for order ORD4471.
Test: LOINC 6301-6, INR, drawn 10:05. Result: numeric (`NM`), **2.8**,
no units (INR is a ratio), reference range 0.8–1.1, flag **H**, status
**F** (final).

`OBX-11 = P` would mean preliminary — and a later `C` (corrected) would
replace it. Downstream systems must handle the correction, not just append
it. That's a classic interface failure: the corrected value arrives, and
the old one is still the one displayed.

### 5.4 ORM / OML — orders (briefly)

`ORM^O01` (legacy) and `OML^O21` (lab-specific, newer) carry **orders** —
the request that eventually produces an ORU. Order in, result out.

### 5.5 MDM — Medical Document Management

Transcribed and generated documents: discharge summaries, op notes,
consult letters.

| Event | Meaning |
|---|---|
| T01 | Original document notification — *a document exists* (no content) |
| **T02** | Original document notification **and content** — the document is in the message |
| T04 | Document status change (e.g., signed) |
| T08 / T10 | Edit / replace |

Structure: `MSH, EVN, PID, PV1, TXA, {OBX}` — TXA describes the document
(type, author, status, unique ID); OBX segments carry the text, line by
line (`TX`), or the whole file encoded (`ED`).

This is the family an AI documentation product lives on: a scribe or
summarizer that writes notes back into a hospital EHR frequently does it
as MDM^T02 through the interface engine.

### 5.6 ACK — acknowledgment

Every message that arrives is acknowledged. The ACK echoes the original's
control ID so the sender can match them.

```
MSH|^~\&|EHR|SUNHOSP|LIS|SUNHOSP|20261006113001||ACK^R01^ACK|ACK90012|P|2.5.1
MSA|AA|LAB88231
```

"EHR received message LAB88231 and accepted it."

| MSA-1 | Mode | Meaning |
|---|---|---|
| **AA** | original | Application Accept — processed |
| **AE** | original | Application Error — understood, but failed (e.g., unknown patient) |
| **AR** | original | Application Reject — refused (bad structure, wrong version, wrong destination) |
| CA / CE / CR | enhanced | Commit Accept / Error / Reject — "I've safely *stored* it," said before processing |

**Original mode** (most common) gives one ACK after the receiver
processes the message. **Enhanced mode** (controlled by MSH-15/MSH-16)
can separate "I've received and stored it" (commit ACK) from "I've
processed it" (application ACK).

A sender that doesn't get an ACK **resends**. That makes duplicates normal
— the receiver should use MSH-10 to recognize a message it has already
processed. Your `book_appointment` idempotency is the same instinct.

---

## 6. Reading a message cold — a drill

When someone hands you a message you've never seen, read it in this
order. It takes about thirty seconds once it's habit.

1. **MSH-9** — what kind of event? (`SIU^S12` → a new booking.)
2. **MSH-12** — which version? (Determines field meanings for the rare
   fields that moved between versions.)
3. **MSH-3/4 → MSH-5/6** — who's talking to whom? (Tells you the business
   context.)
4. **MSH-11** — `P`? Is this production data?
5. **PID-3 and PID-5** — who is the patient, and whose ID is it?
6. **The payload segment for that type** — SCH for SIU, OBR/OBX for ORU,
   PV1 for ADT, TXA for MDM.
7. **Anything starting with Z** — what's site-specific here?

Then ask the questions an integrator asks: *What happens if this message
arrives twice? Out of order? With an empty field versus `""`? Without an
offset on the timestamp?*

---

## 7. Transport: MLLP

v2 defines the *message*, not the network. In hospitals, the network is
almost always **MLLP — Minimal Lower Layer Protocol** over a plain TCP
socket.

### 7.1 Framing

TCP is a byte stream with no message boundaries, so MLLP adds them:

```
<VT> message <FS><CR>
0x0B  …      0x1C 0x0D
```

- **Start block:** `0x0B` (vertical tab)
- **The message:** segments separated by `\r`
- **End block:** `0x1C` (file separator) followed by `0x0D` (carriage
  return)

That's the entire protocol. A receiver reads bytes until it sees
`0x1C 0x0D`, strips the framing, parses, and sends back an ACK in the
same framing.

### 7.2 What MLLP doesn't give you

- **No encryption.** MLLP is plaintext. Hospitals protect it with
  network-level controls — a VPN tunnel to the vendor, a private network
  link, or TLS wrapped around the socket. Any v2 interface to an outside
  vendor starts with a VPN/firewall project.
- **No authentication.** Whoever can open the socket can send messages.
  Security is at the network layer.
- **No built-in parallelism.** Interfaces are typically **one connection,
  one message at a time, in order**, waiting for each ACK. That's
  deliberate: if an A08 (update) is processed before the A04 (register) it
  updates, the update fails or corrupts. The cost: **one bad message can
  block the queue** — the sender keeps retrying it, and everything behind
  it waits. Unsticking a blocked interface queue is a routine part of
  hospital interface operations.

### 7.3 Other transports

Files dropped on SFTP (batch, often nightly), HTTP POST, message queues,
and database tables are all used. MLLP over TCP is the default for real
time.

---

## 8. Interface engines

### 8.1 The problem they solve

A mid-sized hospital runs dozens of clinical systems — registration, EHR,
lab, radiology, pharmacy, cardiology, billing, the patient portal. If
every system talks directly to every other, that's a *point-to-point*
mesh: with *n* systems, up to *n × (n − 1)* connections, each with its own
quirks. Unmanageable.

An **interface engine** is a hub: every system connects once, to the
engine, and the engine routes. *n* connections instead of *n²*.

### 8.2 What an engine actually does

- **Routing** — "ADT messages go to lab, radiology, pharmacy and billing;
  SIU messages go to the reminder service."
- **Filtering** — "only send the reminder service outpatient appointments
  for clinics X and Y."
- **Transformation** — the main job. Receiving system wants the MRN in
  PID-2 instead of PID-3, a different date format, `F`/`M` mapped to its own
  codes, a Z-segment added or removed, version 2.3 converted to 2.5.1.
- **Code translation** — local lab codes ↔ LOINC, local department codes ↔
  the receiver's locations.
- **Protocol conversion** — MLLP in, file out; v2 in, FHIR out; database
  poll in, HTTP POST out.
- **Queuing and retry** — store-and-forward when a destination is down,
  replay after it recovers.
- **ACK handling** — generate ACKs to senders, manage ACKs from receivers.
- **Monitoring and alerting** — queue depth, errors, stalled interfaces.
  The engine dashboard is how an interface team knows something broke at
  3 a.m.
- **Logging and replay** — every message kept for audit and for
  resending.

### 8.3 The products

| Engine | Note |
|---|---|
| **Rhapsody** | Major commercial engine; the company merged with Corepoint (under the Lyniate name, now Rhapsody) — you'll see both product names |
| **Infor Cloverleaf** | Long-established, common in large health systems |
| **InterSystems HealthShare / IRIS for Health** (formerly Ensemble) | Engine plus data platform; InterSystems also builds a major EHR (TrakCare) |
| **Mirth Connect** (NextGen) | Historically the open-source default — **but since version 4.6 (March 2025) it's proprietary**; 4.5.2 was the last open-source release, and the community fork is called **Open Integration Engine** |
| **Epic Bridges** | Not a standalone engine — Epic's own interface module that sends and receives v2 to and from Epic. Hospitals on Epic usually still run a separate engine in front of it |

The Mirth licence change is recent and still trips people up. If you say
"Mirth is open source" in an interview, you'll be about eighteen months out
of date.

### 8.4 Why "every hospital has one"

Because no hospital can afford to have each vendor build a custom
connection to each other vendor — and because the hospital wants **one
place** to see, control, log and fix its data flows. The engine is owned
by the hospital's **interface team** (integration analysts), and in
practice any outside vendor's integration is a conversation with them:
they decide what messages you get, in what format, on what timeline.

For an FDE that's the practical lesson: **your counterpart for
integration isn't the EHR, it's the interface team and their engine.**

---

## 9. Versions and the reality of v2

### 9.1 Versions

v2.1 through v2.9 exist. In US practice, **2.3 / 2.3.1** (older interfaces)
and **2.5.1** (newer ones — required by federal programs for electronic
lab reporting and immunization reporting) dominate. Later versions exist
but are less common. v2 is designed to be backward-compatible: new versions
add fields and segments at the end; receivers ignore what they don't know.

### 9.2 "If you've seen one HL7 interface…"

…you've seen **one** HL7 interface. The industry saying is accurate. The
standard defines structure; every implementation decides which optional
fields it fills, which codes it uses, which Z-segments it adds, which
required fields it actually leaves empty, and what timezone its timestamps
mean.

That's why every real interface has an **interface specification** — a
document from the sending system listing exactly which segments and fields
it sends and what they mean. Reading it is the first task in any
integration project. Testing against real sample messages from that site
is the second.

Note the parallel with FHIR: *grammar shared, content varies* — exactly
what we discussed about FHIR servers. v2 is the same problem with a
thirty-year head start and less tooling.

---

## 10. v2 and FHIR — how they compare and coexist

| | HL7 v2 | FHIR |
|---|---|---|
| Era | late 1980s → today | 2014 → today |
| Format | pipe-delimited text | JSON (or XML) |
| Model | **messages** about **events** | **resources** with **state** |
| Direction | **push** — sender announces | **pull** — client asks (subscriptions exist but are weakly adopted) |
| Transport | MLLP over TCP, VPN | HTTPS, OAuth2 (SMART) |
| Addressing | by position (PID-5) | by name (`Patient.name`) |
| Extensions | Z-segments | `extension` |
| Where you meet it | inside the hospital, system to system | apps, external partners, patients, regulation |
| Scheduling | **SIU** (notify), SRM (request — rarely implemented) | Schedule / Slot / Appointment |
| Strength | real-time events, everywhere, battle-tested | queryable, web-native, self-describing |

**How they coexist in practice:**

- The EHR keeps its v2 interfaces internally and exposes FHIR externally.
  The FHIR API is often a **façade** over the same database the v2
  interfaces feed.
- The **interface engine translates** between them — v2 in, FHIR out, or
  the reverse. HL7 itself publishes a **V2-to-FHIR** mapping
  implementation guide for exactly this.
- **Real time is still v2.** If you need to *know when* something happens
  — a patient registered, an appointment cancelled — a v2 feed from the
  engine is usually the most reliable source, even in hospitals with
  mature FHIR APIs.

The one-line version for an interview: *v2 announces what happened; FHIR
answers what is.*

---

## 11. Your agent and v2 — the facts your paragraph needs

5.4's Do step 3 asks for one paragraph: *how your agent plugs into a
hospital that only speaks v2.* That paragraph is yours. These are the facts
it has to stand on.

**Fact 1 — SIU is a notification from the filler.** The system that owns
the calendar sends SIU. If the hospital's scheduling system owns the
calendar (it does), **it** emits SIU^S12 after a booking. Your agent
doesn't emit SIU into a hospital that owns its own schedule; it would
*receive* SIU.

**Fact 2 — v2 has a booking request, but almost nobody implements it.**
SRM^S01 is how a placer asks a filler to book. Most scheduling systems
don't accept it. So "book via v2" usually isn't available — the booking has
to go through another door (a vendor API, a FHIR endpoint, a custom
operation, or a human).

**Fact 3 — consuming SIU keeps you in sync.** Even when you book through
another door, the hospital's SIU feed tells you about *every* change to
the schedule — bookings made by staff, cancellations, reschedules, no-shows.
That's how your agent would know a slot it offered five minutes ago was
taken at the front desk.

**Fact 4 — when *you* are the filler, you publish.** If your agent's
system *is* the schedule of record (as your Postgres was before module 5),
then other systems learn about your bookings from **your** SIU messages —
S12 on booking, S13 on reschedule, S15 on cancellation.

**Fact 5 — you never connect to the EHR directly.** You connect to the
hospital's **interface engine**, through a VPN, under an interface
specification negotiated with their interface team.

Questions your paragraph should answer: Who is the filler? Which door does
the booking go through? Which feed tells you about changes? What carries
the appointment ID across both worlds (SCH-2 ↔ your FHIR `Appointment.id`
↔ your Postgres shadow)? What happens when the SIU arrives before your own
booking call returns?

That last one is the race from 5.3, in v2 clothing. Mention it.

---

## 12. Dental reality, and the EP angle

### 12.1 Dental

Most dental practice-management systems — Dentrix, Eaglesoft, Open
Dental, Curve, Denticon — **don't speak HL7 v2 at all**, just as most don't
speak FHIR. Independent dental practices sit outside the hospital
interface world entirely; integration goes through vendor APIs (Open
Dental's REST API) or partner programs.

The exception: dental clinics **inside** hospital systems and academic
centres run on the hospital's EHR (Epic has a dental module), and there,
v2 applies like anywhere else.

For your product, that means v2 is a **hospital-market** skill, not a
dental-market one. Valuable for your FDE positioning; not on the critical
path for iHeartSmiles.

### 12.2 The EP angle — v2 is how pacemakers report

This is where v2 meets your clinical background directly.

Implanted cardiac devices — pacemakers, ICDs, CRT devices — report
through manufacturer remote-monitoring platforms. The IHE profile for
getting that data into clinical systems is **IDCO (Implantable Device –
Cardiac – Observation)**, and it's built on **HL7 v2 ORU^R01**: device
interrogation results arrive as OBX segments, coded with the **IEEE 11073
implantable-device nomenclature** — lead impedances, battery status,
arrhythmia episodes, pacing percentages, each one a coded observation.

So the remote-monitoring companies on your Tier 1-EP list — the ones
whose job is moving device data into EHRs and triaging alerts — are, at
the interface level, **ORU^R01 companies**. Being able to say *"device
data arrives as IDCO-profiled ORU^R01, OBX-coded in IEEE 11073; the work is
normalizing across manufacturers and mapping it into the EHR"* is a
conversation almost no software candidate can have — and almost no
clinician can either. Verify the current IDCO specifics against IHE's
cardiology technical framework before you lean on details.

---

## 13. Doing 5.4 in Python — practical notes

Both libraries were installed and run against the messages above.

### 13.1 `python-hl7` (`pip install hl7`)

Simple, forgiving parser — good for reading.

```python
import hl7
msg = hl7.parse(raw)                   # raw must use \r between segments
msh = msg.segment("MSH")
msh[9]                                 # ADT^A04^ADT_A01   (HL7 numbering: MSH[1] is '|')
pid = msg.segment("PID")
pid[5][0][0], pid[5][0][1]             # 'Doe', 'Jane'  (field → repetition → component)
pid[3][0][0]                           # '100245'
```

Indexing goes field → repetition → component. Watch for: `msg["PID.F5"]`
returned only the first component (`Doe`) in my test, not the whole name —
check what an accessor returns before trusting it.

### 13.2 `hl7apy` (`pip install hl7apy`)

Version-aware, can validate against the standard — good for generating
and for strict checks.

```python
from hl7apy.parser import parse_message
from hl7apy.consts import VALIDATION_LEVEL

m = parse_message(raw, find_groups=True)                       # tolerant by default
m.pid.pid_5.value                                              # 'Doe^Jane^M'
m.pid.pid_5.xpn_1.value                                        # 'Doe'
m.pid.pid_7.value                                              # '19870314'

strict = parse_message(raw, validation_level=VALIDATION_LEVEL.STRICT, find_groups=True)
strict.validate()                                              # raises on missing required items
```

### 13.3 Gotchas I hit, so you don't

1. **`\r`, not `\n`.** With `\n`, `python-hl7` returned a single MSH
   segment and **no error**. Normalize first.
2. **RGS is required** before AIS/AIL/AIP in SIU^S12. Strict validation
   fails without it; tolerant parsing doesn't notice.
3. **SCH-6, SCH-16, SCH-20 are required** in 2.5.1 — strict validation
   failed on each, one at a time.
4. **Tolerant parsing hides defects.** Generate with strict validation on,
   so your SIU is valid by the standard — then relax only where a real
   receiver's spec says so.
5. **Count pipes with code, not eyes.** Build segments from a dict of
   field-number → value and join, rather than typing `||||||` by hand.
6. **Escape what you generate.** A note containing `&` or `|` must be
   encoded (§2.4) or it will break the structure.
7. **Put the code under `experiments/hl7v2/`**, as the DoD says — it's a
   learning artifact, not product.

---

## 14. For the interview

### 14.1 The 90-second answer — "v2 versus FHIR?"

Build it from §10; a shape to aim for:

> HL7 v2 is the messaging standard hospitals have run on since the
> nineties, and it's still how most clinical data moves inside a hospital.
> It's event-driven and push-based: when a patient is registered, an ADT
> message goes out; when an appointment is booked, an SIU; when a lab
> result is ready, an ORU. Messages are pipe-delimited segments, sent over
> MLLP through an interface engine like Rhapsody or Cloverleaf that every
> hospital runs. FHIR is the modern REST API — resources you query when you
> need them, the basis of the app ecosystem and of federal certification.
> They coexist: FHIR is usually the front door for external apps, v2 is
> still the plumbing inside, and the interface engine translates between
> them. If you need to know *when* something happens in real time, you're
> often still subscribing to a v2 feed.

### 14.2 Questions to expect

- *"Have you worked with HL7 v2?"* — after 5.4: **"At reading level: I
  know the segment structure and the message families, and I've parsed ADT
  and ORU messages and generated a validated SIU^S12. I haven't operated a
  production interface or an interface engine."** That's the honest-scope
  answer, and it's strong.
- *"What message would you get when an appointment is booked?"* — SIU^S12,
  from the scheduling system as filler; S13 reschedule, S15 cancel, S26
  no-show.
- *"How do you get data out of a hospital in real time?"* — a v2 feed
  through their interface engine, over a VPN, under an interface spec — and
  why FHIR subscriptions usually aren't the answer yet.
- *"What can go wrong with an interface?"* — out-of-order messages, a
  blocked queue behind one bad message, duplicates after resends, patient
  merges (A40) not handled, corrected results (`OBX-11=C`) appended instead
  of replacing, empty vs `""`, timestamps without an offset.

### 14.3 Things not to say

- "FHIR replaced v2." It didn't.
- "Mirth is open source." Not since 4.6.
- "I've built HL7 interfaces." You've parsed and generated messages.
  Different claim.

---

## 15. Glossary

- **Segment** — one line of a message, identified by a three-letter code.
- **Field / component / subcomponent** — `|`, `^`, `&` levels; addressed
  as `PID-5.1`.
- **Trigger event** — what happened (`A04`, `S12`, `R01`).
- **Message structure** — which segments to expect (`ADT_A01`).
- **Placer / filler** — who requests vs who owns and fulfils.
- **ACK / MSA** — acknowledgment; AA / AE / AR.
- **MLLP** — `0x0B … 0x1C 0x0D` framing over TCP.
- **Interface engine** — the hospital's integration hub.
- **Interface specification** — the site-specific document saying what a
  sender actually sends.
- **Z-segment** — a site-specific custom segment.
- **MPI** — Master Patient Index; matches identifiers across systems.
- **IDCO** — IHE profile for implantable cardiac device data over ORU.

---

## 16. Sources

Standards (access to the full HL7 v2 specification requires free HL7
registration):

- HL7 Version 2 product brief — https://www.hl7.org/implement/standards/product_brief.cfm?product_id=185
- Caristix HL7 v2 reference (browsable segment/field definitions by version) — https://hl7-definition.caristix.com/v2/
- HL7 V2-to-FHIR implementation guide — https://build.fhir.org/ig/HL7/v2-to-fhir/
- IHE Cardiology technical framework (IDCO) — https://www.ihe.net/resources/technical_frameworks/#cardiology

Tools:

- python-hl7 — https://python-hl7.readthedocs.io/
- hl7apy — https://crs4.github.io/hl7apy/

Interface engines:

- Mirth Connect licence change (4.6 proprietary; 4.5.2 last open source; Open Integration Engine fork) — https://www.meditecs.com/kb/mirth-connect-license-change/
- Mirth Connect alternatives overview — https://saga-it.com/blog/mirth-connect-alternatives
