"""Protocol parsing/building, checked against Twilio's documented message
shapes verbatim (https://www.twilio.com/docs/voice/media-streams/websocket-messages),
not against our own assumptions about them.
"""

from experiments.raw_media_streams.protocol import (
    build_clear_message,
    build_media_message,
    parse_event,
    parse_media,
    parse_start,
)

START_EVENT = {
    "event": "start",
    "sequenceNumber": "1",
    "start": {
        "accountSid": "ACaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "streamSid": "MZbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "callSid": "CAcccccccccccccccccccccccccccccccc",
        "tracks": ["inbound"],
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        "customParameters": {},
    },
    "streamSid": "MZbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
}

MEDIA_EVENT = {
    "event": "media",
    "sequenceNumber": "3",
    "media": {"track": "inbound", "chunk": "1", "timestamp": "5", "payload": "base64encodedaudio..."},
    "streamSid": "MZbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
}


def test_parse_event_reads_bare_json():
    assert parse_event('{"event": "connected"}') == {"event": "connected"}


def test_parse_start_reads_the_handshake():
    start = parse_start(START_EVENT)

    assert start.stream_sid == "MZbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert start.call_sid == "CAcccccccccccccccccccccccccccccccc"
    assert start.encoding == "audio/x-mulaw"
    assert start.sample_rate == 8000
    assert start.channels == 1


def test_parse_media_reads_track_chunk_timestamp_payload():
    frame = parse_media(MEDIA_EVENT)

    assert frame.track == "inbound"
    assert frame.chunk == 1
    assert frame.timestamp_ms == 5
    assert frame.payload_b64 == "base64encodedaudio..."


def test_build_media_message_carries_no_track():
    # A message TO Twilio never disambiguates inbound/outbound — there's only
    # one leg we can address.
    import json

    built = json.loads(build_media_message("MZstream", "cGF5bG9hZA=="))

    assert built == {
        "event": "media",
        "streamSid": "MZstream",
        "media": {"payload": "cGF5bG9hZA=="},
    }


def test_build_clear_message_shape():
    import json

    assert json.loads(build_clear_message("MZstream")) == {"event": "clear", "streamSid": "MZstream"}
