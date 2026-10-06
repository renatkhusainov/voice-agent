"""A hand-rolled Twilio Media Streams echo server: no Pipecat, no transport,
no serializer, no LLM, and — deliberately — no dependency on app/ at all.

This is a note to myself about what the bare wire protocol requires, not a
feature of the product; that's why it lives in experiments/ instead of app/.
Write-up: docs/notes/raw-media-streams.md.

Run it standalone, on its own port, separately from the real app:

    uvicorn experiments.raw_media_streams.server:app --port 8001

Then point a Twilio number's voice webhook (via its own ngrok tunnel) at
``https://<ngrok-host>/inbound``. It echoes the caller's own audio back to
them and logs one summary line per call: duration and measured frames/sec.
"""

import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from loguru import logger

from experiments.raw_media_streams.protocol import (
    build_media_message,
    parse_event,
    parse_media,
    parse_start,
)

app = FastAPI()

# Twilio paces inbound audio at one frame per ~20ms of mu-law/8000 audio, i.e.
# ~50 frames/sec. The protocol docs don't state this as a promise, so it's a
# number to sanity-check the measured rate against, not something to code
# against as a guarantee.
EXPECTED_FRAMES_PER_SEC = 50


@app.post("/inbound")
async def inbound(request: Request) -> Response:
    host = request.headers.get("host")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws" />
    </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()

    stream_sid: str | None = None
    call_sid: str | None = None
    frame_count = 0
    started_at: float | None = None
    summarized = False

    def summarize() -> None:
        # No transcript, no caller number, no audio in this line — only what
        # identifies and measures the call. A pure echo never has text to
        # leak in the first place, but the habit is the point of the note.
        nonlocal summarized
        if started_at is None or summarized:
            return
        summarized = True
        duration = time.monotonic() - started_at
        fps = frame_count / duration if duration > 0 else 0.0
        logger.info(
            "Raw stream call_sid={} ended: duration={:.1f}s frames={} frames/sec={:.1f} "
            "(~{} expected)",
            call_sid, duration, frame_count, fps, EXPECTED_FRAMES_PER_SEC,
        )

    try:
        while True:
            raw = await websocket.receive_text()
            message = parse_event(raw)
            event = message.get("event")

            if event == "start":
                start = parse_start(message)
                stream_sid, call_sid = start.stream_sid, start.call_sid
                started_at = time.monotonic()
                logger.info(
                    "Raw stream call_sid={} started: encoding={} sample_rate={} channels={}",
                    call_sid, start.encoding, start.sample_rate, start.channels,
                )

            elif event == "media":
                frame_count += 1
                frame = parse_media(message)
                # The entire "echo": forward the same base64 mu-law payload we
                # were just handed straight back out, unmodified. This only
                # works because Twilio's inbound and outbound formats are both
                # mulaw/8000/mono for this stream — a framework buys you the
                # conversion step this skips; nothing here checks the formats
                # still match, or would notice if Twilio ever changed them.
                await websocket.send_text(build_media_message(stream_sid, frame.payload_b64))

            elif event == "stop":
                summarize()
                break

            elif event == "connected":
                logger.debug("Raw stream: Twilio handshake acknowledged")

            else:
                # "mark" echoed back, "dtmf" from a keypress, or a future
                # event type — nothing here reacts to either.
                logger.debug(f"Raw stream: unhandled event type {event!r}")

    except WebSocketDisconnect:
        # "stop" isn't guaranteed to arrive before the socket just closes — a
        # dropped call, a network blip, Twilio restarting. No reconnect logic
        # exists here (docs/notes/raw-media-streams.md): the call is simply
        # over, and this except block is the only place that gets logged.
        summarize()
