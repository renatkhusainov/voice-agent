# HL7 v2: parse two, generate one (lesson 5.4)

The code lives in `experiments/hl7v2/`. It's an experiment: the agent speaks
FHIR, and in production an interface engine would do this translation.
Background reading is in [hl7v2-reading.md](hl7v2-reading.md).

```bash
python -m experiments.hl7v2.hl7                       # both samples + SIU from the captured booking
python -m experiments.hl7v2.hl7 --appointment 1350    # SIU from the live HAPI booking instead
pytest experiments/hl7v2/tests                        # offline, 7 tests
```

| What | File | Result |
|---|---|---|
| Parse ADT^A04 | `samples/adt_a04.hl7` | Garcia, Maria, DOB 1982-03-14, outpatient |
| Parse ORU^R01 | `samples/oru_r01.hl7` | Same patient; OBX-1 INR = **2.4** {INR}, range 2.0–3.0, flag N |
| Generate SIU^S12 | from `samples/fhir_booking_1350.json` (5.3's booking, Appointment/1350) | Validates against 2.5.1 SIU_S12, re-parses to the same IDs, times, patient, location, practitioner |

All three use `hl7apy` against the 2.5.1 structures with STRICT validation,
and every parse is followed by `msg.validate()`. More on that below.

## Six message types

| Message | Fires when | Segments that matter |
|---|---|---|
| **ADT^A04** register a patient | Patient registered for an outpatient visit or check-in. In a dental office this is the "patient arrived" event. | **PID** (MRN in PID-3, name PID-5, DOB PID-7, phone PID-13), **PV1** (PV1-2 class `O`, PV1-7 attending), EVN (when it happened). Uses the `ADT_A01` structure. |
| **ADT^A08** update patient info | Any demographic change: new phone, address, insurance. The most common ADT by volume. | **PID** in full (replace, don't merge), PV1, IN1 (insurance). The receiver overwrites its copy. |
| **ADT^A40** merge patient | Two MRNs found to be the same person; the duplicate is folded into the survivor. | **PID-3** (the surviving ID) + **MRG-1** (the retired ID). Anything keyed on the old MRN has to be re-pointed: our Lead/Appointment rows, FHIR Patient links. |
| **SIU^S12** new appointment | The filler, the system that owns the calendar, books an appointment. **S13** reschedule, **S14** modify, **S15** cancel, **S26** no-show use the same layout. | **SCH** (SCH-1 placer ID, SCH-2 filler ID, SCH-11/TQ1 start and end, SCH-25 status), **PID**, then per resource **RGS** + **AIS** (service), **AIL** (location/chair), **AIP** (provider). |
| **ORU^R01** observation result | A lab or device finalizes a result: an INR before an extraction, pathology on a biopsy. | **OBR** (what was ordered, OBR-4), **OBX** (OBX-3 LOINC code, OBX-5 value, OBX-6 units, OBX-7 range, OBX-8 abnormal flag, OBX-11 status `F`/`C`), PID. |
| **ACK** acknowledgment | Back to the sender for **every** message received, on the same MLLP connection. | **MSA** (MSA-1 `AA` accepted / `AE` error / `AR` rejected, MSA-2 = the original MSH-10), ERR when not AA. Without an AA the sender retries or queues; ordered feeds stall. |

The others you'd meet: SRM^S01 (a placer *asks* a filler to book; rarely
implemented), ORM/OML (orders), MDM^T02 (documents), DFT (charges).

## How the agent plugs into a v2-only hospital

We never connect to the hospital's EHR. We connect to its interface engine
(Mirth/NextGen Connect, Rhapsody, Cloverleaf), over a VPN, with MLLP on a
port their team opens for us. Every message we send gets an ACK, and we
ACK every message we receive. The interface spec states which fields they
actually fill. Who owns the calendar decides which way SIU flows. In
**this** project HAPI is the system of record, so we are the filler: on each
booking an engine channel (or a small adapter like `build_siu_s12`) turns
the FHIR Appointment into SIU^S12. Reschedule and cancel become S13 and S15,
and we publish them to the hospital. In a hospital the hospital owns the
calendar, so the flow reverses. The booking goes in through whatever door
they offer: SRM^S01 if they support it (few do), otherwise a vendor API or a
FHIR facade on the engine. We **consume** their SIU feed into our FHIR
store, so availability reflects what staff book at the desk too. The agent
then says "you're booked" only after the filler confirms it, either with
the SIU^S12 or with an SRR plus an ACK. The appointment carries the same
identity across all three worlds: SCH-1 is our placer ID (the call), SCH-2
is their filler ID, and our Postgres shadow keeps both. That also covers
the 5.3 race in v2 clothing. If their SIU^S12 arrives before our own
booking call returns, we upsert on the filler ID, so it lands as the same
appointment rather than a duplicate. An S15 for a slot we just offered
means "taken": re-offer, the same as a 409 from HAPI.

## The generated SIU^S12

From Appointment/1350 (Dana Lee, cleaning with Sam Rivera RDH, 2026-10-06
14:00Z). One segment per line here; on the wire they're separated by `\r`.

```
MSH|^~\&|FDE_VOICE_AGENT|SUNSHINE_DENTAL|PMS|SUNSHINE_DENTAL|20261002105615-0400||SIU^S12^SIU_S12|SIU0001|T|2.5.1
SCH|1^FDE_VOICE_AGENT|1350^FHIR||||NEW^New appointment^LOCAL|cleaning^cleaning^LOCAL|Normal^Routine schedule request^HL70277|30|min^minutes^UCUM|^^^20261006100000-0400^20261006103000-0400|||||FRONTDESK^Front Desk||^WPN^PH^^1^555^0009999||FDE_VOICE_AGENT^Voice Agent|||||Booked^Booked^HL70278
TQ1|1||||||20261006100000-0400|20261006103000-0400
PID|1||1271^^^FHIR^PI||Lee^Dana||||||||^PRN^CP^^1^813^5550142
RGS|1|A
AIS|1|A|cleaning^cleaning^LOCAL|20261006100000-0400|||30|min^minutes^UCUM||Booked
AIL|1|A|1026^^^SUNSHINE_DENTAL^^^^^Sunshine Dental|O^Provider's office^HL70305||20261006100000-0400|||30|min^minutes^UCUM||Booked
AIP|1|A|1110^Rivera^Sam^^^^RDH|HYG^Dental hygienist^LOCAL||20261006100000-0400|||30|min^minutes^UCUM||Booked
```

| FHIR | → HL7 v2 | Note |
|---|---|---|
| `Appointment.identifier` (sid/call) | SCH-1 placer ID | Who asked: our call |
| `Appointment.id` | SCH-2 filler ID | Who holds it. Assigning authority `FHIR` |
| `start` / `end` | SCH-11 (TQ-4/5), TQ1-7/8, AIS-4, AIL-6, AIP-6 | Converted from UTC to the Location's timezone extension, **with the offset**: a v2 receiver that assumes local time gets 10:00, not 14:00 |
| `end − start` | SCH-9/10, AIS-7/8, AIL-9/10, AIP-9/10 | 30 min |
| `status` booked | SCH-25 `Booked` (HL70278), AIx filler status | |
| `serviceType` | SCH-7, AIS-3 | Our own code system, so `LOCAL`. A real interface maps to their codes (or CDT) |
| Patient id / name / telecom | PID-3 (`PI`, authority `FHIR`), PID-5, PID-13 | Their system matches on its own MRN, not ours. Expect an ADT^A04 or a lookup to give us their PID-3 |
| Location | AIL-3 (PL: point of care, facility, description), AIL-4 `O` | |
| Practitioner + qualification | AIP-3 (XCN with degree in XCN-7), AIP-4 role | |
| Organization telecom | SCH-18 filler contact phone | |

## What I learned doing it

- **STRICT parsing doesn't check required fields.** A message with PID-5
  emptied parses without complaint under `VALIDATION_LEVEL.STRICT`. Strict
  checks structure and datatypes; only `msg.validate()` checks
  cardinality. A receiver needs both. The test
  `test_validation_rejects_a_message_missing_a_required_field` pins this.
- **2.5.1 SCH has three required fields, not one.** Besides SCH-6 (event
  reason) there are SCH-16 (filler contact person) and SCH-20 (entered by
  person). `validate()` found them one at a time. AIL/AIP/RGS only require
  their set IDs, so a "valid" SIU can say almost nothing. Validity is the
  floor; the interface spec is the real contract.
- **SCH-11 vs TQ1.** The TQ datatype in SCH-11 is deprecated from 2.5 in
  favour of TQ1. Receivers built on 2.3 still read SCH-11, so the message
  carries both.
- **Message structure ≠ trigger.** ADT^A04 is parsed with the `ADT_A01`
  structure, and MSH-9.3 says so. Without `find_groups=True` hl7apy can't
  place PID inside `SIU_S12_PATIENT` or OBX inside its observation group.
- **Timestamps carry offsets.** `20261006100000-0400` is unambiguous;
  `20261006100000` means "local to whoever reads it". Always send the offset.
- **Sample files use `\n` for readability.** `load()` converts to `\r`, the
  real segment terminator. Feeding `\n` to a strict parser is the classic
  first bug.
