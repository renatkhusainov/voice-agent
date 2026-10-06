"""Twilio Media Streams wire protocol, hand-parsed.

No Pipecat, no transport abstraction, no serializer — this module exists to
feel exactly what a framework like Pipecat's `TwilioFrameSerializer` and
`WorkerRunner` normally hide from app/services/bot.py. What that turned out to
mean is written up in docs/notes/raw-media-streams.md.

Message shapes are Twilio's, not invented here:
https://www.twilio.com/docs/voice/media-streams/websocket-messages
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MediaFrame",
    "StreamStart",
    "build_clear_message",
    "build_media_message",
    "parse_event",
    "parse_media",
    "parse_start",
]


@dataclass(frozen=True)
class StreamStart:
    """Parsed "start" event. This is the entire handshake: nothing before it
    tells you the stream's identity, and nothing enforces that you hold onto
    `stream_sid` — every message you send back has to carry it yourself."""

    stream_sid: str
    call_sid: str
    encoding: str
    sample_rate: int
    channels: int


@dataclass(frozen=True)
class MediaFrame:
    """One inbound audio chunk.

    `payload_b64` is still base64-encoded mu-law audio: decoding it (and
    re-encoding anything sent back) is on us. A pure echo skips the decode
    step entirely — see build_media_message — which is itself worth noticing:
    it only works because the inbound and outbound formats happen to match.
    """

    track: str
    chunk: int
    timestamp_ms: int
    payload_b64: str


def parse_event(raw: str) -> dict[str, Any]:
    """The one thing Twilio guarantees about a frame: it's JSON with an
    `event` field. Everything else depends on reading that field first, then
    reaching into a differently-shaped sub-object per event type."""
    return json.loads(raw)


def parse_start(message: dict[str, Any]) -> StreamStart:
    start = message["start"]
    fmt = start["mediaFormat"]
    return StreamStart(
        stream_sid=start["streamSid"],
        call_sid=start["callSid"],
        encoding=fmt["encoding"],
        sample_rate=fmt["sampleRate"],
        channels=fmt["channels"],
    )


def parse_media(message: dict[str, Any]) -> MediaFrame:
    media = message["media"]
    return MediaFrame(
        track=media["track"],
        chunk=int(media["chunk"]),
        timestamp_ms=int(media["timestamp"]),
        payload_b64=media["payload"],
    )


def build_media_message(stream_sid: str, payload_b64: str) -> str:
    """A message TO Twilio carries no `track`: we only ever address the
    outbound leg, so there's nothing to disambiguate."""
    return json.dumps({
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": payload_b64},
    })


def build_clear_message(stream_sid: str) -> str:
    """Empties Twilio's outbound playback buffer — this single message is the
    entire mechanism barge-in is built on. Nothing here calls it (an echo has
    no bot speech of its own to interrupt), but a turn-taking bot has to call
    it itself, the instant it notices the caller talking over playback, or the
    caller keeps hearing whatever was already queued before they interrupted.
    """
    return json.dumps({"event": "clear", "streamSid": stream_sid})
