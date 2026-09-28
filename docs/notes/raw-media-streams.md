# Raw Media Streams: what the bare protocol makes you handle

`experiments/raw_media_streams/server.py` and `.../protocol.py` are a Twilio
Media Streams echo server with no Pipecat in it — just a FastAPI `WebSocket`
and `json.loads`. It lives under `experiments/`, not `app/`, and isn't wired
into the product app at all: it's a note to myself about the wire protocol,
not a product feature. The production call path (`app/routers/twilio.py` +
`app/services/bot.py`) hands the same wire protocol to Pipecat's
`create_transport` and `TwilioFrameSerializer` instead. Building the raw
version first is the point: you can't see what a framework is doing for you
until you've done it yourself and felt what's missing.

Verified live: a real WebSocket client, paced at Twilio's real 20ms-per-frame
rate, echoed 50 frames and got each one back byte-identical. Log line from
that run:

```
Raw stream call_sid=CAlive started: encoding=audio/x-mulaw sample_rate=8000 channels=1
Raw stream call_sid=CAlive ended: duration=1.1s frames=50 frames/sec=45.8 (~50 expected)
```

What's *not* verified: whether it's audible as an actual echo over a real
phone call. That needs running the experiment on its own port
(`uvicorn experiments.raw_media_streams.server:app --port 8001`), its own
`ngrok http 8001` tunnel (separate from the product app's), a Twilio number's
voice webhook pointed at `https://<ngrok-host>/inbound`, and a phone. I can't place that
call or hear the result — only the wire-level round trip above.

## Five things, by what breaks if you skip them

### 1. The wire format: framing, base64, and mu-law are three separate facts you have to know at once

Every inbound `media` event is one **frame**: ~20ms of audio, base64-**encoded**,
and the bytes underneath that encoding are **mu-law**, not PCM. None of that is
optional to know, and nothing in the JSON forces you to get it right:

- Forget it's base64 and you feed raw bytes to a decoder: garbage.
- Forget it's mu-law (an 8-bit *logarithmic* companded encoding, not linear
  PCM) and try to resample or mix it like PCM: garbage that sounds like
  garbage, not an error.
- The pure echo in `server.py` skips the mu-law question entirely — it never
  decodes anything, just forwards the same base64 text back out. That's only
  correct because Twilio's inbound and outbound formats happen to match
  (`audio/x-mulaw`, 8000 Hz, mono, confirmed in the `start` event). The moment
  you need to *do* anything with the audio — mix it, run VAD on it, resample
  it for a model — you're decoding mu-law and re-encoding it back, correctly,
  yourself.

Pipecat's `DeepgramSTTService`/`DeepgramTTSService` and the transport layer do
this conversion internally; `app/services/bot.py` never mentions mu-law once.

### 2. The handshake: stream identity is a first message, not a connection property

A WebSocket connecting doesn't tell you anything about *which call* this is.
`streamSid` and `callSid` only exist once the `start` event arrives — the
second message, after `connected` — and after that, every single message you
send back has to carry `streamSid` yourself (`build_media_message` takes it as
a parameter; there's no ambient "current call" to read it from). Get the
handshake order wrong — try to echo audio before `start` has been parsed — and
you either crash on a `None` or send Twilio a message it can't route.

`experiments/raw_media_streams/protocol.py`'s `parse_event` → dispatch-on-`event` structure
*is* the state machine Pipecat's transport auto-detection
(`create_transport`, `parse_telephony_websocket`) builds once and reuses for
Twilio, Telnyx, Plivo and Exotel. Hand-rolled, you build a new copy of this
per-provider.

### 3. Backpressure: nothing stops you from writing faster than Twilio can play

`server.py`'s WebSocket handler echoes every frame the instant it arrives, with a bare
`await websocket.send_text(...)` — no queue, no rate limit, no check on
whether Twilio's side is keeping up. For a 1:1 echo this happens to be safe,
because we only ever send exactly as fast as we receive. It stops being safe
the moment a real bot needs to speak a multi-second reply: nothing here would
stop you from queueing an entire TTS response into the socket faster than
20ms/frame, and nothing tells you when Twilio's playback buffer is full versus
draining normally. Pipecat's transport and pipeline queue (`PipelineWorker`,
`transport.output()`) pace outbound frames and expose backpressure as part of
the frame-processing model; here, there's a `websocket.send_text` and your own
judgment.

### 4. Turn-taking is three separate, unbuilt capabilities: VAD, turn detection, and barge-in

The raw handler doesn't know when the caller starts or stops talking (no
**VAD**), doesn't know when it's the bot's turn to respond (no **turn
detection**), and has no way to stop mid-sentence when the caller interrupts
(no **barge-in**) — because an echo server needs none of them; it just mirrors
whatever arrives. The moment there's a bot with something to say, all three
become required, and the protocol gives you exactly one primitive to build
them on: the `clear` message (`build_clear_message`, unused in this echo),
which empties Twilio's outbound buffer on command. Everything else — deciding
*when* to send `clear`, detecting speech in a stream of mu-law frames,
deciding a pause means "done talking" versus "thinking" — is audio processing
you'd write from raw samples.

Pipecat's `SileroVADAnalyzer` and `LLMUserAggregatorParams`'s turn-detection
strategies (`app/services/bot.py`) are exactly this, already built.

### 5. Nothing survives a disconnect

`server.py` has a `try`/`except WebSocketDisconnect` for exactly one reason: a
call can end by `stop` arriving *or* by the socket just closing — dropped
call, network blip, Twilio restarting a media server — and the protocol
doesn't guarantee `stop` comes first. Miss the `except`, and half of all real
calls never get a summary logged at all (confirmed by
`test_disconnect_without_a_stop_event_still_logs_a_summary`). And that's the
ceiling of what this file does about failure: there is no reconnect, no
session resumption, no idle timeout, no retry. One dropped WebSocket is one
lost call, permanently.

`app/services/bot.py`'s `run_bot` wraps the entire pipeline in `try/finally`
for the same reason — so a `Call` row never gets stuck open — but also gets,
for free from Pipecat's `WorkerRunner`/`PipelineWorker`: an idle timeout
(`pipeline_idle_timeout_secs`), `on_pipeline_error`/`on_pipeline_timeout`
hooks, and SIGINT handling. None of that exists here. It's the same shape of
problem (something ended; make sure the record reflects it), solved once
per-project here versus once, generically, in the framework.
