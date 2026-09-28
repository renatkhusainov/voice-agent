# HIPAA design: a voice agent for dental front desks

**Status:** design document for a course/portfolio project, not a compliance
certification. **No BAA is currently signed with any vendor named below, and
this system must not process real patient data until the "What production
requires" section is closed out.** This is not legal advice — a real deployment
needs a lawyer and a security review, not just this document.

**Audited:** 2026-09-22, against commit range on `feat/call-lifecycle-and-transcripts`.
**Code:** [`app/phi/redact.py`](../app/phi/redact.py), [`app/phi/log_scrub.py`](../app/phi/log_scrub.py),
[`app/services/calls.py`](../app/services/calls.py). Implementation-level detail
lives in [`docs/phi-and-secrets.md`](phi-and-secrets.md); this document is the
policy and architecture view built on top of it.

---

## 1. What this system does

A caller dials a dental practice's number. Twilio answers, looks the number up
against a `Practice` row, and if it matches, opens a Media Stream WebSocket
into this app. A Pipecat pipeline turns the caller's audio into text
(Deepgram), generates a reply (Anthropic), and speaks it back (Deepgram TTS).
Each finalized turn — what the caller said, what the assistant said — is
written to a `transcript_turns` table, redacted first. The call itself is
tracked in a `calls` row from connect to hang-up.

So PHI moves through four parties: the caller (audio), Twilio (telephony and
the media stream), Deepgram (speech-to-text and text-to-speech), and Anthropic
(the model deciding what to say). It's stored in one place: this app's
Postgres database, plus, currently, raw `.wav` files on local disk.

## 2. PHI classification

*A caller's phone number and anything they say to a health care office about
their own or their child's condition is PHI under HIPAA once it's tied to that
identity — this holds for a dental practice's calls the same as any other
covered entity's.*

| Where | Field | PHI? | Why |
|---|---|---|---|
| `calls.caller_number` | phone number | **Yes** | Identifies the patient/caller; tied to a health care encounter (a call to a dental office) |
| `calls.started_at` / `ended_at` | timestamps | **Yes** (as metadata) | Combined with `caller_number`, reveals when this person contacted a health care provider |
| `calls.recording_url`, `app/temp/recordings/*.wav` | call audio | **Yes — the most sensitive item in the system** | Raw voice, unredacted; **cannot** be masked by a text regex |
| `transcript_turns.text` | what was said | **Yes, before redaction** | Names, callback numbers, dates of birth, symptoms, insurance details |
| `transcript_turns.role`, `created_at` | metadata | No on its own | Only "user" or "assistant" and a timestamp |
| `leads.name`, `callback_number`, `notes` | lead capture | **Yes** | Directly identifying, plus `notes` is free text about why they called |
| `leads.intent` | enum | Borderline | `appointment` / `question` / `callback` / `other` — low information alone, but see below |
| `appointments.requested_slot`, `confirmation` | scheduling | **Yes** | A booked slot ties an identity to "this person is getting dental treatment on this date" |
| `practices.*` | practice info | No | Business information, not patient information |

`leads` and `appointments` are modeled but have **no API routes yet** — there's
no way to write or read one today. They're in this table because the schema
exists and the classification has to hold the moment those routes ship, not
retrofitted after.

## 3. The redaction decision

Two independent layers, because each catches what the other structurally can't:

**Deepgram (`redact=["pci", "ssn"]` on the STT connection).** Deepgram's
`redact` option covers digit sequences (`numbers`), 50+ named entities in
English (`pii`, `phi`, or individually — `ssn`, `dob`, `phone_number`,
`name`, ...), and payment data (`pci`). We ask for `pci` and `ssn` only. A
front-desk agent has no legitimate use for a card number or a Social Security
number, so those are removed before they reach our process, the LLM, the
database, or a log line — the earliest point they can be. We deliberately do
**not** ask for `numbers` or `phi`: the agent has to hear the caller's name,
callback number, requested date, and reason for calling to do its job, or it
can't book anything. This is unverified against a live socket — see
`docs/phi-and-secrets.md` for that caveat.

**Our own boundary (`app/phi/redact.py`, applied inside `add_transcript_turn`,
the only function that constructs a `TranscriptTurn`).** After the LLM has used
the raw text, everything written to the database is masked: phone numbers, SSN
shapes, dates with a year, and — this is the detail that matters most for a
voice product — **spoken digit runs**. Deepgram's number redaction and most
regex examples assume digits already look like digits ("813-555-0142"). A
voice conversation doesn't: the caller says "eight one three, five five five,
zero one four two," and the assistant reads it back the same way to confirm
it. A digit-shaped-only matcher would store exactly the number it was supposed
to hide. `redact()` masks runs of 7+ digit words for this reason, with a
worked example and the full masking rules in `docs/phi-and-secrets.md`.

**What this doesn't catch (yet):** insurance IDs (explicitly next), names,
addresses, and dates spelled out in words. Those need either better patterns
or a small NER pass, not built yet.

**A real incident, because it's the honest way to show this actually works.**
Partway through building this, a live call arrived before a database migration
had been applied. The `transcript_turns` insert failed with `UndefinedTable`,
and the *database driver's own error message* included the bound INSERT
parameters — the assistant's full greeting, verbatim, in the application log.
Nothing in `redact()` was wrong; the leak was one layer up, in how SQLAlchemy
reports a failed query. That's what `hide_parameters=True` on the engine and
the log scrubber in `app/phi/log_scrub.py` exist to close, and it's why this
document's logging claims are backed by tests that capture real log output
from a real error, not just unit tests of the masking function in isolation.

## 4. BAA map

*HIPAA requires a signed Business Associate Agreement with any vendor that
creates, receives, maintains, or transmits PHI on the covered entity's behalf,
**before** any PHI is sent to them — not after. None of the three are signed
for this project.*

| Vendor | What they'd touch | BAA available? | Requirements | How obtained | Status here |
|---|---|---|---|---|---|
| **Twilio** | Caller audio, phone numbers, call metadata (Programmable Voice + Media Streams) | Yes | **Security or Enterprise Edition account** — not available on standard accounts. Only "HIPAA Eligible Products" may carry PHI; Programmable Voice, Phone Numbers, and Media Streams are on that list, but must be re-verified against Twilio's current eligible-products list before launch | Not self-serve — contact an Account Rep or Sales | ❌ Not signed. Current account tier not verified |
| **Deepgram** | Audio → transcript, transcript → audio (STT/TTS) | Yes, "upon request" | No published account-tier gate in their docs (unlike Twilio), but not self-serve | Contact Sales / Account Executive | ❌ Not signed |
| **Anthropic** | Raw transcript text sent to the model | Yes, but narrowly | Only the **first-party API** (sales-enabled) or **Claude Enterprise** — never Free/Pro/Max/Team. Covered Models require **30-day data retention** and are incompatible with Zero Data Retention. **The specific model this app calls, `claude-haiku-4-5-20251001`, has not been confirmed against Anthropic's current Covered Models list** | Org's Primary Owner signs the BAA, then Sales enables it on the account | ❌ Not signed. Model coverage unverified |

**What "not signed" means in practice:** every one of these vendors' Terms of
Service disclaims BAA-equivalent protection by default. Sending real PHI to
any of them right now — including through this very codebase — would be an
impermissible disclosure. The `.env.example` in this repo provisions API keys
with no BAA gate of any kind, which is correct for a dev/test project using
synthetic data and would be wrong for anything else.

## 5. Retention policy

**Current state, stated plainly: there is none.** Nothing in this codebase
expires, archives, or deletes a `Call`, a `TranscriptTurn`, or a recording.
Every row and every `.wav` file persists until someone manually deletes it.
That is a gap, not a policy, and it's listed again in Section 6.

**Proposed policy** (design target, not yet implemented):

| Data | Proposed retention | Reasoning |
|---|---|---|
| `transcript_turns.text` (already redacted) | 90 days, then hard delete | Long enough for the practice to resolve a dispute or complaint about a call; short because it's still PHI even redacted (names, dates, complaints survive) |
| `calls` row (metadata: timestamps, status, practice) | 7 years | Aligns with typical dental-record retention requirements in most US states, which run independently of HIPAA (HIPAA itself sets no fixed retention period — it requires *a* documented, enforced policy, not a specific number) |
| Call audio (`.wav`) | 30 days, then hard delete, or don't record by default | Highest-risk artifact in the system (Section 2) and currently the least protected — unencrypted on local disk, no lifecycle management. Recommend disabling the recorder for anything but explicit QA needs until it's moved to encrypted, access-controlled storage with a lifecycle policy |
| `leads` / `appointments` (once built) | Practice's own scheduling retention, likely years | Operationally required for the business, same reasoning as the `calls` row |

**Who can read them today: anyone who can reach the API or the database.**
There is no authentication on any endpoint — `GET /calls`, `GET
/calls/{id}/transcript`, and `GET /practices` are all open. This is the
single largest gap between this project and something a real patient's data
could touch; it's restated in Section 6 because it can't be buried in a table.

**Who should be able to read them, once access control exists:**

| Data | Who |
|---|---|
| A practice's own calls and transcripts | Staff at that practice, authenticated, scoped to `practice_id` |
| Call audio | Same, plus tighter logging of every access (it's the most sensitive artifact) |
| Cross-practice data | Nobody by default; a platform-admin role only, and that access itself should be audit-logged |
| Raw (unredacted) transcript text | No one, ever, through this system — by design, it's never stored. Anthropic sees it in-flight per the BAA above, and that's the only place it exists after the call ends |

## 6. What production requires

Ordered by what would block launch first.

**Legal / contracts**
- [ ] Signed BAA with Twilio (Security or Enterprise Edition account first)
- [ ] Signed BAA with Deepgram
- [ ] Signed BAA with Anthropic, **with `claude-haiku-4-5-20251001` (or whatever
      model is live) confirmed as a Covered Model** — if it isn't, either the
      model changes or PHI cannot reach it
- [ ] A designated HIPAA Privacy/Security Officer for the organization running this (an organizational requirement, not a code change)
- [ ] A breach notification plan

**Access control — currently the biggest gap in the code itself**
- [ ] Authentication on every endpoint (there is none today)
- [ ] Authorization scoped by practice — a practice must only see its own calls
- [ ] An audit trail of *who* read *which* call/transcript and *when* (HIPAA's
      Security Rule requires audit controls; the operational logging built in
      this project deliberately excludes content, which is correct for
      operational logs but is not an access audit trail — a different thing)

**Data protection**
- [ ] Encryption at rest for the database (`caller_number`, `leads.notes`,
      redacted transcript text, and anything in `leads`/`appointments` once built)
- [ ] Move call recordings off local disk to encrypted object storage with a
      lifecycle policy, or stop recording by default (Section 5)
- [ ] Enforce TLS end-to-end in production; today TLS termination is handled
      by whatever sits in front of `uvicorn` (ngrok in dev) and has not been
      verified as enforced end-to-end for a production deployment
- [ ] Rebuild and redistribute the Docker image — an earlier build baked real
      call recordings into the image layer before `.dockerignore` was fixed;
      anyone holding that old image has that audio

**Retention and deletion**
- [ ] Implement the policy in Section 5 as actual scheduled deletion, not a document
- [ ] A way to honor a patient's request to access or delete their own data —
      no such interface exists yet

**Redaction gaps**
- [ ] Insurance IDs (next, per `redact.py`'s own docstring)
- [ ] Names and addresses (not regex-tractable; likely needs NER)
- [ ] Card numbers at our own layer as a backstop to Deepgram's `pci`

**Organizational**
- [ ] Workforce HIPAA training
- [ ] A minimum-necessary access policy, enforced by the RBAC above, not just written down
- [ ] A risk assessment covering the four vendors and this codebase together, not each in isolation

## 7. How the claims in this document were checked

So a reader can tell what's been verified against running code versus what's
carried over from vendor documentation:

- **Verified against a live instance:** the redaction boundary being the only
  write path (`grep` confirms one construction site for `TranscriptTurn`), a
  simulated end-to-end call whose stored transcript is masked and whose
  captured log output contains no digits or transcript content
  (`tests/test_call_e2e.py`), uvicorn's actual access-log line for a real
  request, and a forced 500 error's traceback with parameters hidden.
- **Verified by test suite, not a live socket:** the 58 `redact()` cases in
  `tests/test_phi_redact.py`, the log-scrubbing cases in
  `tests/test_log_scrub.py`, and the secrets audit in `tests/test_secrets.py`.
- **Derived from vendor documentation, not independently tested:** the BAA
  requirements in Section 4 (Twilio's HIPAA-eligible products page and
  changelog, Deepgram's trust/security page, Anthropic's BAA and Covered
  Models help-center articles), and Deepgram's `redact` parameter coverage in
  Section 3. Vendor docs and BAA terms change — re-verify before relying on
  this table for a real decision.
- **Not implemented, stated as gaps, not claims:** everything in Sections 5's
  "current state" and all of Section 6.
