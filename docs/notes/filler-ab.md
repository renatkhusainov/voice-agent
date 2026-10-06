# Filler A/B: "Let me check that for you"

**Status: not run yet. It needs ten real phone calls.** This page is the
protocol plus what was already measured without a phone.

## What's already known (measured, not guessed)

Tool execution time on a live pipeline (local Postgres + Redis, real Claude
choosing the tools, `app/agent/live.py`'s own `took_ms` log):

| Run | Tool | took_ms |
|---|---|---|
| booking smoke | book_appointment (proposal) | 66 |
| booking smoke | book_appointment (confirmed) | 89 |
| booking smoke (earlier run) | book_appointment | 63, 40 |
| escalation smoke | escalate_to_human | 51 |

**All of these are far below the 300 ms threshold, so with local
infrastructure the filler will almost never fire.** An A/B run as specified
may show no difference, simply because arm A never says the filler. That's a
result, not a failure.

The silence a caller actually notices on a tool turn is mostly *not* the
tool. It's the second LLM round trip after the tool result: about 1 s TTFB
plus TTS, per `latency-baseline.md`. Covering that gap would mean starting the
timer at `on_function_calls_started` and stopping it at the first text of the
post-tool reply. That's a different experiment. It's noted here, not built,
because the spec says ">300 ms tool".

## Protocol

Same script for all ten calls: ask for a cleaning on a specific day, give a
name and number, say yes to the read-back.

**Arm A (filler on):** `TOOL_FILLER_ENABLED=true` (default), restart the
server, make 5 calls.
**Arm B (filler off):** `TOOL_FILLER_ENABLED=false`, restart, make 5 calls.

To make the filler fire in arm A with fast local tools, you can also run a
variant with `TOOL_FILLER_DELAY_MS=50`. Record it as its own arm.

Per call, from the log:

```
Call <id> agent: tool_calls=… tool_ms=[…] fillers_spoken=… filler_enabled=… response_latency_ms=[…]
```

`response_latency_ms` is user-stopped-speaking → bot-started-speaking per turn
(Pipecat's `UserBotLatencyObserver`), the objective half. The subjective half
is your own "did that pause feel awkward?" (1 = fine, 5 = I thought it hung up).

## Results

| # | Arm | fillers_spoken | tool_ms | response_latency_ms (tool turns) | Felt (1–5) | Notes |
|---|---|---|---|---|---|---|
| 1 | A | | | | | |
| 2 | A | | | | | |
| 3 | A | | | | | |
| 4 | A | | | | | |
| 5 | A | | | | | |
| 6 | B | | | | | |
| 7 | B | | | | | |
| 8 | B | | | | | |
| 9 | B | | | | | |
| 10 | B | | | | | |

**Perceived difference:** _to fill in after the calls._
