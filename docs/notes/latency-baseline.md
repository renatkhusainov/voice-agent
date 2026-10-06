# Response-latency baseline

Five real, live measurements against the actual production dependencies — not
mocked, not simulated. Each hits the real Anthropic and Deepgram APIs over the
network, using the exact model, voice, and system prompt `app/services/bot.py`
uses. No API key value was ever printed or logged while producing this: the
script reads them from `.env` and only the resulting numbers left this
process.

**What "response latency" means here:** the time from *the caller's utterance
being fully known as text* to *audio starting to play back* — i.e. LLM
time-to-first-byte (TTFB) plus TTS time-to-first-byte. That's the piece this
app controls and the piece a caller actually perceives as "how fast does it
answer." It does **not** include Deepgram's own STT finalization delay
(discussed below), Twilio network round-trip, or Pipecat's internal queueing —
see Limitations.

## Methodology

- **LLM:** `POST https://api.anthropic.com/v1/messages`, `model:
  claude-haiku-4-5-20251001`, same `system` prompt as `bot.py`, streamed.
  TTFB = time to the first `content_block_delta` SSE event.
- **TTS:** `POST https://api.deepgram.com/v1/speak`, `model=aura-asteria-en`
  (what `bot.py`'s `voice="aura-asteria-en"` setting actually puts on the wire
  — Pipecat's `DeepgramTTSService` sends `voice`, not the `model="aura-2.0"`
  field, as the query param; see `docs/notes/raw-media-streams.md`'s sibling
  note on reading a framework's source rather than assuming). `encoding=mulaw,
  sample_rate=8000`, matching `PipelineParams(audio_out_sample_rate=8000)`.
  To keep TTS timing comparable across the five runs, every run synthesizes
  the same fixed acknowledgement sentence rather than each LLM's own
  (different-length) reply — isolating the variable being measured (network +
  server latency) from text length.
- **Five caller utterances**, representative of real front-desk calls:
  insurance question, same-day injury, hours question, reschedule request,
  age-eligibility question.
- One-second gap between runs; no retries, no warmup call discarded.

## The five measurements

| # | Caller said | LLM TTFB | LLM total | TTS TTFB | TTS total | **Response latency** |
|---|---|--:|--:|--:|--:|--:|
| 1 | "Do you take Delta Dental insurance?" | 1482 ms | 1570 ms | 543 ms | 635 ms | **2025 ms** |
| 2 | "My son chipped his tooth, can we come in today?" | 607 ms | 1183 ms | 350 ms | 441 ms | **957 ms** |
| 3 | "What are your hours on Saturday?" | 602 ms | 900 ms | 313 ms | 404 ms | **915 ms** |
| 4 | "Reschedule my daughter's cleaning to Thursday." | 916 ms | 1192 ms | 330 ms | 420 ms | **1246 ms** |
| 5 | "Do you see kids under two years old?" | 763 ms | 1149 ms | 327 ms | 416 ms | **1090 ms** |

**Response latency (LLM TTFB + TTS TTFB): mean 1247 ms, median 1090 ms, min
915 ms, max 2025 ms, stdev 406 ms.**

## Reading it

- **The LLM dominates.** Median LLM TTFB (763 ms) is roughly 2.3x median TTS
  TTFB (330 ms). If this needs to get faster, the LLM call is where the time
  is — not the voice.
- **Run 1 is the outlier** (2025 ms, almost double the median), driven
  entirely by LLM TTFB (1482 ms vs. 602–916 ms on the other four). This is the
  first request the script made; a cold TLS/HTTP connection to Anthropic is
  the likely explanation, not a representative steady-state number. Runs 2–5,
  after the connection was warm, cluster much tighter (915–1246 ms). A
  production pipeline that holds its connections open across the whole call
  (which Pipecat's persistent WebSocket/HTTP clients do) shouldn't pay this
  per-turn — but it *will* pay it once, on the very first turn of every call,
  which is exactly the turn a caller is most sensitive to ("did it hear me?").
- **TTS TTFB is small and consistent** (313–543 ms) — synthesizing a short
  fixed sentence isn't where a fix needs to go.

## STT: real recorded call audio, not synthetic

Five `.wav` files already on disk from earlier real calls
(`app/temp/recordings/`, produced by this project's own `AudioBufferProcessor`)
sent to Deepgram's **prerecorded** endpoint, same `model=nova-3-general` and
`redact=[pci, ssn]` as `bot.py`'s live STT settings. Only timing, audio
duration, and a transcript **character count** are reported below — the
transcript text itself was never printed or stored anywhere by this
measurement.

| File | Audio duration | Processing time | Realtime factor | Transcript length |
|---|--:|--:|--:|--:|
| recording_1_20260918_123125.wav | 21.7 s | 858 ms | 0.040 | 321 chars |
| recording_20260916_123115.wav | 16.9 s | 1140 ms | 0.068 | 238 chars |
| recording_20260916_123357.wav | 7.7 s | 607 ms | 0.079 | 100 chars |
| recording_20260916_151134.wav | 2.6 s | 733 ms | 0.286 | 17 chars |
| recording_20260916_152626.wav | 0.9 s | 358 ms | 0.398 | 0 chars (silence) |

Realtime factor (processing time ÷ audio duration) stays well under 1 across
every file — Deepgram transcribes 21.7s of audio in 858ms, a factor of 0.04.
STT processing throughput itself is not a bottleneck.

**This is not the same number as live per-utterance STT latency**, and
shouldn't be read as one: a prerecorded call processes an entire file at once,
while a live call streams audio and has to decide, frame by frame, *whether
the caller has stopped talking* before it can finalize anything — that
decision is **endpointing**, a fixed silence-duration wait, not a processing
cost. `bot.py` doesn't override it, so Deepgram's platform default applies;
their docs state that default as 10ms of silence, though that figure wasn't
independently reproduced here and is worth re-verifying against a live
streaming session before relying on it — it's surprisingly short for
conversational speech (https://developers.deepgram.com/docs/endpointing).

## Limitations — what this baseline does not measure

- **Not a phone call.** This hits Anthropic's and Deepgram's HTTP/WS APIs
  directly from a dev machine. It excludes Twilio's network round-trip, real
  microphone/codec audio (vs. a text prompt or a prerecorded file), Pipecat's
  own pipeline/queueing overhead, and the live STT endpointing wait discussed
  above. "Caller stops talking → caller hears the reply" in a real call is
  this response latency **plus** all of that.
- **Five runs, one machine, one time of day.** Not a distribution — a
  baseline. Network conditions, Anthropic/Deepgram load, and geography all
  move these numbers; re-run before trusting an SLA on them.
- **TTS used a fixed sentence, not each LLM's actual reply.** Deliberate, to
  isolate the variable under test, but it means TTS total time here
  underestimates what a longer real reply would take to fully synthesize
  (TTFB, the number that matters for perceived latency, is unaffected by
  reply length).
