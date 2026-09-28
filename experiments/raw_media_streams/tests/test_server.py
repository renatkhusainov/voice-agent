"""Drives the raw echo WebSocket the way Twilio actually would: the exact
"connected" -> "start" -> "media"* -> "stop" event sequence and shapes from
https://www.twilio.com/docs/voice/media-streams/websocket-messages, sent over
a real WebSocket test session against the real route (experiments/raw_media_streams/server.py)
— not a mock of it.

This is the closest thing to "hear your own voice echoed back" that can be
checked without a phone: byte-for-byte payload equality between what a
simulated caller sends and what the server sends back, in order, with the
right streamSid on every message. Whether it's audible over an actual call
needs an actual call (see docs/notes/raw-media-streams.md).
"""

import base64
import re
import xml.etree.ElementTree as ET

STREAM_SID = "MZ00000000000000000000000000000000"
CALL_SID = "CA00000000000000000000000000000000"


def start_event(stream_sid=STREAM_SID, call_sid=CALL_SID):
    return {
        "event": "start",
        "sequenceNumber": "1",
        "start": {
            "accountSid": "ACfake",
            "streamSid": stream_sid,
            "callSid": call_sid,
            "tracks": ["inbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            "customParameters": {},
        },
        "streamSid": stream_sid,
    }


def media_event(chunk, payload_b64, stream_sid=STREAM_SID):
    return {
        "event": "media",
        "sequenceNumber": str(chunk + 1),
        "media": {"track": "inbound", "chunk": str(chunk), "timestamp": str(chunk * 20), "payload": payload_b64},
        "streamSid": stream_sid,
    }


def stop_event(stream_sid=STREAM_SID, call_sid=CALL_SID):
    return {
        "event": "stop",
        "sequenceNumber": "99",
        "stop": {"accountSid": "ACfake", "callSid": call_sid},
        "streamSid": stream_sid,
    }


def fake_frame(i: int) -> str:
    """20ms of stand-in mu-law audio, base64-encoded like a real payload —
    distinct bytes per frame so echo order/identity is actually checked."""
    return base64.b64encode(bytes([i % 256]) * 160).decode()


# ── TwiML ────────────────────────────────────────────────────────────────────
def test_raw_inbound_twiml_points_at_the_raw_websocket_no_parameters(client):
    response = client.post("/inbound", data={"From": "+18135550142", "To": "+18135551234"})

    root = ET.fromstring(response.content)
    stream = root.find("Connect/Stream")
    assert stream.get("url").endswith("/ws")
    # Unlike /twilio/inbound, this path looks nothing up — no practice_id to pass.
    assert stream.find("Parameter") is None


# ── The echo itself ────────────────────────────────────────────────────────
def test_each_inbound_frame_is_echoed_back_unmodified_in_order(client):
    payloads = [fake_frame(i) for i in range(5)]

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        ws.send_json(start_event())

        echoed = []
        for i, payload in enumerate(payloads):
            ws.send_json(media_event(i, payload))
            echoed.append(ws.receive_json())

        ws.send_json(stop_event())

    assert [e["event"] for e in echoed] == ["media"] * 5
    assert [e["streamSid"] for e in echoed] == [STREAM_SID] * 5
    assert [e["media"]["payload"] for e in echoed] == payloads
    # What Twilio's own outbound shape omits vs. what it sent us:
    assert "track" not in echoed[0]["media"]


def test_a_frame_before_start_is_not_confused_with_one_after(client):
    # Order matters: the handshake has to land before any audio is meaningful.
    with client.websocket_connect("/ws") as ws:
        ws.send_json(start_event(stream_sid="MZfirst"))
        ws.send_json(media_event(0, fake_frame(0), stream_sid="MZfirst"))
        first_echo = ws.receive_json()
        ws.send_json(stop_event(stream_sid="MZfirst"))

    assert first_echo["streamSid"] == "MZfirst"


# ── The log line: measured frames/sec and total duration ──────────────────
def test_stop_event_logs_duration_and_frame_rate(client, log_messages):
    with client.websocket_connect("/ws") as ws:
        ws.send_json(start_event())
        for i in range(7):
            ws.send_json(media_event(i, fake_frame(i)))
            ws.receive_json()
        ws.send_json(stop_event())

    summary = next(m for m in log_messages if m.startswith(f"Raw stream call_sid={CALL_SID} ended"))
    assert re.search(r"duration=\d+\.\d+s", summary)
    assert "frames=7" in summary
    assert re.search(r"frames/sec=\d+\.\d+", summary)


def test_disconnect_without_a_stop_event_still_logs_a_summary(client, log_messages):
    # Twilio does not guarantee "stop" arrives before the socket just closes.
    with client.websocket_connect("/ws") as ws:
        ws.send_json(start_event())
        ws.send_json(media_event(0, fake_frame(0)))
        ws.receive_json()
        # No stop_event() — exit the `with` block, which drops the connection.

    assert any(m.startswith(f"Raw stream call_sid={CALL_SID} ended") for m in log_messages)


# ── What must never show up in the log ──────────────────────────────────────
def test_log_output_never_contains_a_payload(client, log_messages):
    weird_payload = fake_frame(255)  # arbitrary bytes, the closest thing to "content" here

    with client.websocket_connect("/ws") as ws:
        ws.send_json(start_event())
        ws.send_json(media_event(0, weird_payload))
        ws.receive_json()
        ws.send_json(stop_event())

    assert not any(weird_payload in m for m in log_messages)
