# Tools on a live call

The five agent tools, the confirmation gate and `DialogState` now run on a
real Twilio call, not only in the text harness. Code: `app/agent/live.py`,
wired in `app/services/bot.py`.

## How a tool call flows on a call

```
caller speaks ─► STT ─► user aggregator ─► DialogStateLLMService._process_context
                                              │  1. LiveCallAgent.system_prompt_for(context)
                                              │     - caller turns = count of "user" messages in the context
                                              │     - load DialogState from Redis (key call:<call_id>)
                                              │     - build_system_prompt(practice, state)  ← booking progress
                                              │  2. Anthropic streams a reply, maybe with tool_use
                                              ▼
                                   Pipecat calls LiveCallAgent.handle_tool_call(params)
                                              │  - FillerTimer starts (unless disabled / escalation)
                                              │  - asyncio.to_thread:
                                              │      load state → build_gated_dispatch → execute_tool
                                              │      → save state → write role=tool transcript row
                                              │  - result_callback(result)  → Pipecat runs inference again
                                              │    (escalation: run_llm=False, speak handoff, EndWorkerFrame)
                                              ▼
                                           TTS ─► caller
```

| Requirement | Where |
|---|---|
| Tools registered with the Anthropic service; handlers call `tools.py` | `LiveCallAgent.register`, `handle_tool_call`, which goes through `loop.execute_tool`, the same function the text harness uses |
| Sync DB work in a thread | `asyncio.to_thread(self._run_tool_blocking, …)`: DB and Redis both run there, with a short-lived `SessionLocal()` per call |
| State per call: `call_id` → Redis | `state_key(call_id) = "call:<id>"`; the prefix keeps it from colliding with a text-mode `session_id` |
| Pipeline reads state each turn for the system prompt | `DialogStateLLMService._process_context` runs before every inference, including the one after a tool result |
| Filler when a tool takes >300 ms | `FillerTimer`; `TOOL_FILLER_ENABLED`, `TOOL_FILLER_DELAY_MS` |
| Escalation: handoff line, Lead, graceful end | `escalate_to_human` → Lead(intent=callback, notes=reason); `live.py` speaks `ESCALATION_MESSAGE` verbatim with `run_llm=False`, then `EndWorkerFrame` |
| Read-back formatting in the tool output, not the prompt | `app/agent/spoken.py`; `AvailableSlot.spoken`, `*.spoken_time`, `DialogState.read_back_text(tz)` |

## Decisions worth knowing

**The gate's turn counter is derived, not incremented.** In the text harness,
`take_turn` adds 1 per HTTP request. On a call, Pipecat can run inference more
than once for the same caller message: after every tool result, and on a
speculative run it later discards. A per-inference `+= 1` would let
`book_appointment` confirm itself with no one saying "yes". So the counter is
set to *the number of caller messages in the context*, and re-running
inference on the same context gives the same number. Mutation-tested: switch
it back to `+= 1` and two tests in `tests/test_live.py` fail.

**Escalation doesn't give the model another turn.** After
`escalate_to_human` succeeds, the result goes back with `run_llm=False`. The
handoff line comes from the tool (`tools.ESCALATION_MESSAGE`), so the model has
no chance to add "that sounds like an infection, try rinsing…". The only
place the model could still add something is text it writes *before* calling
the tool. Prompt v4 tells it to call the tool first, and a live run showed it
doing that (below).

**Slots without an offset are the practice's local time.** This came from a
live run, not a design review. The model sent `2026-12-15T09:30:00` for "9:30
in the morning". The old `AwareDatetime` rejected it with a bare "Invalid
input", and the booking failed. Asking the model to add a UTC offset is
exactly the date math it's bad at, and a wrong offset books the wrong hour.
Now a slot without an offset is defined as practice-local (`tools.localize_slot`),
stored as UTC, and both forms of the same time compare equal in the gate.
Validation errors also name the failing field now (never its value), so the
model can retry.

**No filler before a handoff.** "Let me check that for you" followed by "I'm
sorry you're dealing with that" sounded wrong in a live run.

## Read-back on the phone

| Before | After (`spoken.py`) |
|---|---|
| `time: 2026-12-15T14:30:00+00:00` (UTC, ISO) | `9:30 AM Tuesday the 15th` (practice timezone) |
| `phone ending in 0142` (TTS may say "one hundred forty-two") | `phone number ending in 0 1 4 2` |
| `13:00` | `1 PM` (whole hours drop ":00") |

`check_availability` returns both `start` (to pass back) and `spoken` (to
say). Names are passed through unchanged. Nothing formats a name; if TTS
mispronounces one, that's for a pronunciation dictionary, not string
formatting.

**Still to verify by ear on a real call:** how Deepgram Aura actually
pronounces "9:30 AM Tuesday the 15th" and "0 1 4 2". The tests prove the
*strings*; only a phone call proves the *audio*.

## What was verified, and how

| DoD item | Status |
|---|---|
| Booking end to end: row exists, transcript shows tool calls | ✅ **Real Pipecat pipeline + real Claude + Postgres + Redis, text in place of audio** (scratch smoke script): proposal → verbatim read-back "9:30 AM Tuesday the 15th" → "yes" → Appointment stored 14:30 UTC; `GET /calls/{id}/transcript` shows `role=tool` rows. ⏳ **An actual phone call has not been made**, so Twilio audio, STT and TTS are unverified. |
| "My tooth is killing me and I'm bleeding" → escalates, no advice, Lead, warm ending | ✅ Same smoke setup, real Claude: its first action was `escalate_to_human`, with no text before it. Lead intent=callback with the reason in notes; handoff spoken; pipeline ended itself via `EndWorkerFrame`. ⏳ Not yet on a phone call. |
| Filler A/B, five calls each way | ⏳ Needs real calls, see `filler-ab.md` |

Offline tests (no network): `tests/test_live.py`, `tests/test_spoken.py`,
plus updated `test_tools.py` and `test_state.py`.

## Running a real call

```bash
docker compose up -d postgres redis
alembic upgrade head          # f3b9c2d4e5a1 adds role 'tool' to transcript_turns
uvicorn app.main:app --port 8000   # REDIS_URL defaults to localhost:6379
# expose it (ngrok etc.) and point the Twilio number's voice webhook at /twilio/inbound
```

Then check the call's rows:

```bash
curl localhost:8000/calls/<call_id>/transcript
```

**Redis down doesn't mean a silent call.** A first real call went silent:
the app ran on the host, and the old default `REDIS_URL` (`redis:6379`, a
Docker-only hostname) didn't resolve, so every inference failed before the
model was called. The default is now `localhost`, and Docker compose sets
its own value for the container. On top of that, without Redis the agent
keeps talking, and availability, FAQ and escalation still work. Only
`book_appointment` is refused, and the model is told to offer a callback.

**Only slots the practice offers can be proposed or booked.**
`tools.check_booking` refuses a time in the past, outside business hours or
off the 30-minute grid, and a slot already held by another call. The gate
runs it *before* proposing, so a caller is never read back a time that would
fail after they say yes. `reschedule_appointment` applies the same hours
check. Before this, nothing forced the model to call `check_availability`
first, and "Sunday at 3 AM" would have booked.

**Barge-in during the confirming call can't double-book or strand the
caller.** Pipecat cancels a running tool call when the caller starts talking,
but it can't stop the worker thread, so the booking still commits.
`LiveCallAgent._run_to_completion` waits for that thread (shielded) before it
lets the cancellation through and releases the lock. Booking is also
idempotent per call and slot. The model's retry gets the existing appointment
back, instead of a second read-back ending in "already booked" against the
caller's own booking.

## Known gaps

- `reschedule_appointment` is still not gated (unchanged; see
  `conversation-state-machine.md`).
- `redact()` masks the ISO date inside a tool row as `[DOB]`, so a tool row's
  slot date is hidden too. That's safe, but the transcript is less useful for
  evals. The Appointment row has the real value.
- If the handoff line is interrupted, the call still ends, because
  `EndWorkerFrame` can't be interrupted. That's intended, but check it on a
  real call.
- Whether Twilio has finished playing the last audio before Pipecat's
  serializer hangs up has not been measured.
