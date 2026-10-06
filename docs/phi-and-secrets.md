# PHI and secrets

What this app does to keep caller data and credentials out of the database, the
logs and client-facing errors, why each choice was made, and what is still open.
Code: `app/phi/redact.py`, `app/phi/log_scrub.py`. Audited 2026-09-21.

> **Not a substitute for a compliance review.** Real patient calls send audio and
> text to Twilio, Deepgram and Anthropic. Before any real PHI flows through this,
> each of them needs a signed BAA (or an equivalent agreement) in place. The code
> below limits what *we* store and log; it does not change what vendors receive.

## Where the raw words go

| Stage | Sees raw content? |
|---|---|
| Caller audio → Twilio → Deepgram | Yes (audio). Deepgram masks cards and SSNs in the transcript it returns |
| Transcript → our process | Cards/SSNs already masked by Deepgram; everything else raw |
| LLM (Anthropic) | Raw name, number, dates, complaint. The agent needs them to do its job |
| **Database** (`transcript_turns.text`) | **No.** `redact()` runs inside `add_transcript_turn`, the only write path |
| **Logs** | **No.** Our lines carry ids and counts; everything else passes `LogScrubber` |
| Call audio on disk (`app/temp/recordings`) | Yes. Unredacted, see "Open items" |

## Decision: Deepgram's `redact`. STT level, ours, or both?

**Both, split by what the agent needs to hear.**

What Deepgram's option covers (their docs, Nova streaming `/v1/listen`):

- `numbers` / `true` / `aggressive_numbers`: 3+ digit sequences, including dates,
  account numbers, cards and SSNs (`aggressive_numbers` adds 1-2 digit numbers).
- Entity groups, **English only**: `pci` (card number, expiry, CVV), `pii` (names,
  locations, identifying numbers), `phi` (medical conditions, drugs, injuries),
  plus 50+ single entities (`ssn`, `phone_number`, `dob`, `name`, `credit_card`,
  `email_address`, ...).
- Output is a tag: `[SSN_1]`, `[PHONE_NUMBER_1]`, `[DOB_1]`. Interim results show a
  generic `[REDACTED]` first. Best accuracy needs `no_delay=false` (Pipecat's
  default; we do not override it).
- Flux (`/v2/listen`) supports digits only, no entities. We run `nova-3-general`.
- It masks the transcript, not the audio, and the docs don't say what Deepgram
  retains.

We ask Deepgram for **`pci` and `ssn` only** (`STT_REDACT` in `bot.py`):

- A front-desk agent has no use for a card number or an SSN, so they are removed
  before they reach our process, the LLM, the database or a log line.
- We do **not** ask for `numbers`, `pii` or `phi`. The agent has to hear the
  caller's name, callback number, dates and the reason for the call: `numbers`
  would also blank appointment times ("Tuesday at 1030"), and `phi` would blank
  the complaint, which breaks the "anything clinical: I'll have the office call
  you back" rule. Those are masked at *our* boundary instead, after the LLM has
  used them.

Why not STT-only: it is vendor-side and probabilistic, and the assistant's own
replies never pass through Deepgram. The model routinely repeats a number back
("is that eight one three...?"), and only our layer sees that.
Why not ours-only: a card or SSN should not be forwarded to a second vendor at all.

**Unverified:** `["pci", "ssn"]` matches Deepgram's published entity table and
reaches the connection parameters (tested offline), but it has not been exercised
against a live socket. An invalid value would fail the handshake and every call
with it, so **check the first live call after deploying.**

## Decisions inside `redact()`

- **Spoken digits: we care.** Assistant text and un-normalised STT spell numbers
  out, so a digits-only matcher leaks exactly what people read aloud. Threshold is
  a run of 7 or more digit words/digits (a 7-digit local number is the shortest
  thing worth calling a phone number); "nine one one" and "one two three" are
  speech, not PHI. Separators may be spaces, commas, dots or hyphens, deliberately
  mixed: callers pause between groups, so requiring one consistent separator
  would leak the most realistic case. Cost: a counted list of 7+ digit words
  ("one, two, three, four, five, six, seven") is masked too.
- **Dates:** "DOB-shaped" means a full date with a year. A bare "September 24th"
  or "9/24" is a booking slot the eval suite needs and is left alone. An
  appointment given *with* a year is masked too: same shape as a DOB, and leaking
  a DOB is the worse failure.
- **7-digit local numbers** are masked only with a hyphen or dot ("555-0142");
  seven bare digits are too ambiguous.
- **Digit boundaries everywhere:** a phone/SSN pattern never carves a match out of
  the middle of a longer run, so a 16-digit card number comes back untouched
  rather than half-masked. Masks contain no digits, so `redact()` is idempotent
  and Deepgram's own tags pass through unchanged.

**Known gaps:** insurance IDs (next), card numbers (left to Deepgram `pci`; add a
Luhn matcher if that stops being enough), names, addresses, dates spelled out in
words, compact dates (`03141987`), a year on its own.

## Logging policy

| Risk | Handling | Verified |
|---|---|---|
| Our own lines printing numbers | Router logs `CallSid` + `practice_id`; bot logs `call_id`, role, char count, turn counts | Tests capture our messages *before* scrubbing |
| Pipecat DEBUG (86 call sites interpolate content: the full LLM context, TTS text, `call_data` with the caller's number) | Pipecat records below INFO are dropped | Test |
| Pipecat ERROR with a frame (`exception processing {frame}` prints the frame's text) | Frame payload masked to `text: [<redacted>]` | Test built from real frame classes |
| Tracebacks showing local variable values (loguru `diagnose`) | Off. Traceback rendered without locals | Test |
| SQLAlchemy `[parameters: {...}]` in DB errors (this leaked the assistant's greeting in the missing-table incident) | `hide_parameters=True` on the engine; scrubber backstop; Postgres `DETAIL:` lines hidden | Live: a forced 500 logs `[SQL parameters hidden ...]` |
| uvicorn access log | A POST body is **never** logged: it records client IP, request line and status only (checked against a live server). A GET webhook would put `From=` in the path, so the path is scrubbed | Live + test |
| Log timestamps being read as a DOB | Only message and exception text are scrubbed, not the header | Test |

## Secrets audit

| Question | Result |
|---|---|
| Keys in the repo? | None in tracked files or in any commit on any branch. `.env` is untracked and ignored. `test_no_credential_is_committed` fails the build if a live env secret or a key-shaped string is ever tracked |
| Keys in logs? | Registered secret values (and any `*_KEY/_TOKEN/_SECRET/_PASSWORD` env var) are masked by exact value; Anthropic-key and Twilio-SID shapes are masked even unregistered. No code path logs settings |
| Keys in client errors? | Unhandled errors return a bare `Internal Server Error`; `HTTPException` details are fixed strings; settings validation errors do not echo the value (`hide_input_in_errors`) |
| Keys/PHI in the Docker image? | **Found and fixed.** `.env` was not in the image, but `COPY . .` had baked in real call recordings (`app/temp/recordings/*.wav`) and `.claude/`. Now in `.dockerignore`. **The already-built image still contains them:** rebuild (`docker compose up -d --build`) and do not push or share the old one |
| Other | `Makefile` holds a static ngrok domain: not a secret, but it identifies your dev tunnel |

## Open items

- **Call recordings are raw audio on local disk** and cannot be regex-redacted.
  They need a retention window, encryption at rest and a decision on whether to
  keep them. `calls.recording_url` is not populated yet.
- `calls.caller_number` is stored in clear (needed for callbacks). Restrict who can
  read it and encrypt the database volume.
- BAAs with Twilio, Deepgram and Anthropic before real patients.
- Insurance IDs in `redact()`.
- Rotate anything that was ever pasted into a chat, ticket or terminal recording.
