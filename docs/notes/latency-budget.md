# Latency budget

Where every millisecond of a caller turn goes, with a p50/p95 for each stage,
before and after one round of optimization. All numbers are measured, from
nine runs of 5 calls / 20 caller turns each.

## How it's measured

**Instrumentation:** `app/services/latency.py`, a Pipecat observer on every
live call. It writes one `call_metrics` row per caller turn, keyed
`call_id + turn` and tagged with `LATENCY_LABEL`, plus one structured log
line. Each column is an offset from the same anchor: **when the caller
actually stopped speaking**. That is the VAD's decision time minus its
`stop_secs`, because the VAD only decides 200 ms later. Timestamps are the
pipeline clock at the moment a frame was *pushed*, not when the observer got
round to it.

| Column | Event |
|---|---|
| `vad_ms` | VAD decides speech ended |
| `stt_final_ms` | last final transcript from Deepgram |
| `turn_end_ms` | turn released to the LLM (Smart Turn + STT wait) |
| `llm_start_ms` | first inference starts |
| `llm_first_token_ms` | first text token of any inference in the turn |
| `tools` | each tool's start/end |
| `llm_last_token_ms` | last inference ends |
| `tts_first_byte_ms` | first TTS audio chunk |
| `first_audio_out_ms` | output transport starts sending audio = **end to end** |

**Calls:** `experiments/latency/drive_calls.py` plays *Twilio* against our own
`/twilio/ws`. It uses the real Media Streams protocol and real-time 20 ms
μ-law frames, with caller speech synthesized once by Deepgram (voice
`aura-orion-en`) and silence between utterances. Everything server-side is
production code: serializer, Silero VAD, Smart Turn v3, Deepgram STT, Claude
Haiku 4.5 with tools, Deepgram TTS, output transport. Five scripted calls,
four caller utterances each (booking, FAQ, availability, a reschedule-ish
call, a filling). The driver also measures, client-side, *last voiced frame
sent → first audio frame received*. It agrees with the server's end-to-end
figure to within ~20 ms at p50 in every run, which cross-checks the
instrumentation.

**Not measured:** the PSTN leg (carrier + Twilio edge, typically tens to a
few hundred ms each way), real phone audio (the synthetic caller is clean
TTS speech), and anything but one machine on one day. Real calls write the
same table. Run the server with `LATENCY_LABEL=phone` and use
`python -m experiments.latency.report phone`.

**Read the p95s with care.** With 20 turns, p95 is essentially the
second-slowest turn. Two runs of *identical* code (`stt-250`, `optimized-v2`)
gave end-to-end p95s of 1575 and 2751 ms. p50s are stable to about ±60 ms.
The conclusions below rest on effects that repeat across runs, not on one
p95.

## Baseline

| Stage | p50 | p95 |
|---|--:|--:|
| Endpointing: speech end → turn released | 352 | 354 |
| &nbsp;&nbsp;of which VAD silence window | 200 | 200 |
| &nbsp;&nbsp;STT final transcript (offset) | 296 | 344 |
| Turn released → LLM request | 6 | 9 |
| **LLM request → first token** | **646** | **2356** |
| First token → first TTS audio | 396 | 880 |
| First TTS audio → audio out | 3 | 5 |
| **End to end** | **1519** | **3039** |
| &nbsp;&nbsp;turns without tools | 1288 | 1508 |
| &nbsp;&nbsp;turns with tools | 1896 | 4277 |
| Tool execution (per call) | 52 | 79 |

27 turns, 7 of them interrupted: the caller kept talking after a pause
between sentences, Smart Turn had already released the turn, and the reply
was cut off.

Reading it:
- **Endpointing is a fixed 352 ms.** VAD takes 200 ms. The rest is Pipecat
  waiting out its Deepgram STT safety timer (`DEEPGRAM_TTFS_P99 = 0.35 s`
  from speech end), even though the final transcript was already there at
  ~296 ms. It isn't marked `finalized`, so the timer decides.
- **The LLM is the biggest stage.** Its p50 (~650 ms, Haiku 4.5, ~2–4k
  input tokens) is roughly the model's floor. Its p95 is **tool turns where
  the model calls a tool before saying anything**: the caller waits for two
  full inferences plus the tool call's JSON.
- **"First token → first audio" hides a Deepgram detail** (found below): it
  isn't TTS speed.

## Budget

Target from the brief: end to end p50 ≤ 1.2 s, p95 ≤ 2.0 s. The component
floors make 1.2 s p50 tight but reachable with this stack: VAD 200 ms, STT
~100 ms, Haiku TTFT ~600 ms, TTS ~150 ms after a flush.

| Stage | Budget p50 | Budget p95 | Shipped p50 | Shipped p95 |
|---|--:|--:|--:|--:|
| Endpointing | 350 | 400 | 352 ✅ | 354 ✅ |
| Turn released → LLM request | 10 | 20 | 2 ✅ | 4 ✅ |
| LLM request → first token | 600 | 900 | 611 ≈ | 2405 ❌ |
| First token → first TTS audio | 250 | 450 | 377 ❌ | 503 ≈ |
| First TTS audio → audio out | 10 | 10 | 3 ✅ | 4 ✅ |
| **End to end** | **1200** | **2000** | **1368 ❌** | **3195 ❌** |

Neither end-to-end target is met yet. See "Where the p95 is now" for exactly
what's left.

## What was tried

| # | Change | Stage it targets | Before → after on that stage | Verdict |
|---|---|---|---|---|
| 1 | Anthropic prompt caching (`LLM_PROMPT_CACHING=true`) | LLM TTFT | p50 646 → 648 | ❌ **Didn't help, never engaged** |
| 2 | TTS `TextAggregationMode.TOKEN` | first token → audio | p50 396 → 398 | ❌ **Didn't help** |
| 3 | Prompt v5: short line before any tool call | LLM → first token (tool turns) | model spoke first 7/12 → 11/13 tool turns; audio still late | ◐ Needed, not enough alone |
| 4 | Flush a reply's first sentence (`app/services/tts.py`) | first token → audio | **p95 880 → 418–503** (every flush run) | ✅ **Shipped** |
| 5 | STT wait 0.35 → 0.25 s | endpointing | p50 352 → 290–332 | ✗ Small gain, more cut-off callers: not shipped |

### 1. Prompt caching: didn't help, and why

All 29 inferences in the run showed `cache_read = cache_write = 0`, even
though the Pipecat usage metrics said prompts were 4.3k–7.3k tokens, above
Haiku 4.5's **4096-token** caching minimum. Two findings:

- **Pipecat 1.10 double-counts Anthropic prompt tokens.** It adds
  `input_tokens` from `message_start` *and* again from `message_delta`, which
  now carries the cumulative count too. The real prompts were ~2.2k–3.7k
  tokens (system 627 + tools 1,498 + history), all **below** the minimum. So
  nothing could be cached.
- Verified from both sides. The same request shape sent directly caches fine
  above 4096 tokens (5,456 written, then read). Inside a real Pipecat
  pipeline with a >4096-token history, `DialogStateLLMService` writes
  7,008 tokens and reads them back on the next turn. It's purely prompt
  size.

Caching would matter with a bigger prompt (a real FAQ knowledge base) or a
model with a 512/1024-token minimum. Shipped off.

### 2. TOKEN aggregation: didn't help, and why

Streaming LLM tokens straight to TTS left the stage unchanged (396 → 398 ms)
because **Deepgram's streaming TTS produces no audio for buffered text until
it gets a `Flush`**. Measured directly: a sentence with no Flush produced no
audio in 2.5 s; with a Flush, first audio arrived in 129–145 ms. Pipecat
sends one Flush per reply, when the LLM finishes. Sending tokens earlier just
moves where they wait.

### 3 + 4. Speak before the tool, and flush that sentence

The turn-by-turn data showed the real problem on tool turns. Even when the
model *did* say "Let me check that." first (first token at ~1.0 s), the audio
went out only at ~1.9 s, right after the tool call. Two things held it:

- **Pipecat's sentence aggregator** releases a sentence only after it sees
  the *next* non-space character (to rule out "$29.50"). After a preamble,
  the next thing is a tool call, so the sentence waits for the end of the
  response.
- **Deepgram** then waits for the end-of-reply Flush anyway (see #2).

`app/services/tts.py` fixes both:
- `EagerSentenceAggregator` releases a sentence immediately on `!`/`?`, or on
  a period after an ordinary word. Numbers, initials and "Dr."/"a.m." keep
  Pipecat's lookahead.
- `FlushingDeepgramTTSService` sends a Flush after each reply's first
  sentence, and hides that flush's `Flushed` answer from Pipecat. Pipecat
  treats any `Flushed` as "reply finished" and would otherwise cut the reply
  short.

Same prompt (v5), without → with the flush:

| | tool-preamble | shipped |
|---|--:|--:|
| First token → first TTS audio, p50 / p95 | 424 / 953 | 377 / **503** |
| E2E, turns with tools, p50 | 1942 | **1383** |
| E2E, all turns, p50 | 1530 | **1368** |

The same effect shows in every flush run. Time-to-first-audio p95 was
418–503 ms in all five, against 880–958 ms in all four without. Tool-turn
p50 was 1304–1422 ms against 1783–1942 ms.

**The first version of this broke calls, and the runs caught it.** Flushing
after *every* sentence hit a Deepgram limit found by the `optimized` run:
**20 flushes per ~60 s per connection**. The 21st is refused with
`EXCESSIVE_FLUSH`; measured, the allowance came back ~64 s later. A refused
flush also gets no `sequence_id`, so the bookkeeping drifted. In that run
**2 of 20 caller turns got no reply audio at all**, and 4 replies failed with
"TTS context completed with no audio".

The shipped version flushes only a reply's *first* sentence. Early flushes
stop at 12 per minute, leaving room for Pipecat's end-of-reply flushes.
`Flushed` answers and refusals are matched to sent flushes in order, which
can't drift. Two confirmation runs after the fix: 0 refused flushes, 0 "no
audio" errors, 0 silent turns in 40. Total audio for a fixed 4-sentence reply
is identical with and without it (7,083 vs 7,082 ms), so nothing is cut.

### 5. STT wait 0.25 s: small, and not free

Pipecat's Deepgram timer releases a turn 0.35 s after speech end unless the
transcript is marked finalized. At 0.25 s the turn is released as soon as the
final transcript arrives. Endpointing p50 went from 352 to 290–332 ms. But
interrupted turns ran ~8 per 27 with it against ~6 without: a hint that
callers get cut off mid-utterance more often. 20–60 ms isn't worth that, so
it's off (`STT_TTFS_P99_SECS` still exists for a real-call A/B).

## Before / after (per stage)

Shipped = prompt v5 + first-sentence flush, everything else default.

| Stage | Before p50 | Before p95 | After p50 | After p95 |
|---|--:|--:|--:|--:|
| Endpointing: speech end → turn released | 352 | 354 | 352 | 354 |
| &nbsp;&nbsp;of which VAD silence window | 200 | 200 | 200 | 200 |
| &nbsp;&nbsp;STT final transcript (offset) | 296 | 344 | 290 | 342 |
| Turn released → LLM request | 6 | 9 | 2 | 4 |
| LLM request → first token | 646 | 2356 | 611 | 2405 |
| First token → first TTS audio | 396 | 880 | 377 | **503** |
| First TTS audio → audio out | 3 | 5 | 3 | 4 |
| **End to end (server)** | **1519** | 3039 | **1368** | 3195 |
| &nbsp;&nbsp;turns without tools | 1288 | 1508 | 1324 | 1416 |
| &nbsp;&nbsp;turns with tools | 1896 | 4277 | **1383** | 3456 |
| **End to end (client, last voiced frame → first audio)** | 1514 | 3039 | 1384 | 3178 |
| Turns / interrupted | 27 / 7 | | 27 / 7 | |

## Where the p95 is now

In the shipped run, **both turns over 1.62 s are `book_appointment` calls
the model made without the v5 preamble**. One was the proposal (the caller
gave a time, name and number in one breath), the other the confirmation
("Yes, please book it."); they took 3.2 and 3.7 s. In both, the caller waits
while the model writes every booking field as tool-call JSON (~1.5–2 s),
then for a second inference. Every other turn in that run came back in
≤ 1.62 s.

Next levers, not done here:
- **The confirmation shouldn't repeat what the gate already holds.** A
  "confirm the pending booking" call with no arguments (name, phone, service
  and time are already in DialogState) removes the JSON generation from that
  turn.
- **The preamble shouldn't depend on the model remembering it.** It complied
  on 11 of 13 tool turns; the two misses are exactly the p95. A filler spoken
  by code at tool start doesn't rescue these turns, because the tool only
  starts ~2 s in, after the JSON. It has to happen before the model starts
  the tool call, which means the prompt, or a model that streams text first
  more reliably.

The p50 gap (1368 vs 1200) is the Haiku floor plus endpointing. Levers not
tried: a faster turn-release once Deepgram finalization works, and trimming
the ~1.5k tokens of tool schema.

## All runs

| Run | Config | E2E p50 | E2E p95 | 1st token→audio p95 | interrupted |
|---|---|--:|--:|--:|--:|
| baseline | prompt v4 | 1519 | 3039 | 880 | 7/27 |
| prompt-cache | + caching | 1398 | 2944 | 958 | 5/25 |
| tts-token | + TOKEN aggregation | 1415 | 2302 | 936 | 7/26 |
| tool-preamble | prompt v5 | 1530 | 2595 | 953 | 7/27 |
| sentence-flush | v5 + flush every sentence | 1334 | 2756 | 447 | 5/25 |
| stt-250 | v5 + flush every sentence + STT 0.25 | 1256 | 1575 | 474 | 7/27 |
| optimized | same, as defaults | 1341 | 1895 | 418 | 11/29 (2 silent: flush limit) |
| optimized-v2 | v5 + first-sentence flush + STT 0.25 | 1354 | 2751 | 470 | 9/29 |
| **shipped** | **v5 + first-sentence flush** | **1368** | **3195** | **503** | 7/27 |

## Reproduce

```bash
docker compose up -d postgres redis && alembic upgrade head   # a1c5e7f9b2d4 adds call_metrics
experiments/latency/run.sh my-label [SETTING=value ...]        # restarts the app on :8765, drives 5 calls
python -m experiments.latency.report baseline my-label
```

Settings used above: `LLM_PROMPT_CACHING`, `TTS_TEXT_AGGREGATION`,
`TTS_FLUSH_EACH_SENTENCE`, `STT_TTFS_P99_SECS` (`app/config.py`).
