import asyncio
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService

from app.models.models import Call, CallStatus, Practice, TranscriptRole, TranscriptTurn
from app.services import bot as bot_module
from app.services.calls import add_transcript_turn, end_call, start_call


def add_practice(db_session, phone="+18135551234"):
    practice = Practice(name="Sunshine Dental", timezone="America/New_York", phone=phone)
    db_session.add(practice)
    db_session.commit()
    return practice


# ── start_call ────────────────────────────────────────────────────────────────
def test_start_call_inserts_in_progress_call(db_session):
    practice = add_practice(db_session)

    call = start_call(db_session, practice.id, "+18135550000")

    stored = db_session.get(Call, call.id)
    assert stored.practice_id == practice.id
    assert stored.caller_number == "+18135550000"
    assert stored.status == CallStatus.in_progress
    assert stored.started_at is not None
    assert stored.ended_at is None


# ── add_transcript_turn ─────────────────────────────────────────────────────────
def test_add_transcript_turn_inserts_row(db_session):
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")

    turn = add_transcript_turn(db_session, call.id, TranscriptRole.user, "I need an appointment")

    stored = db_session.get(TranscriptTurn, turn.id)
    assert stored.call_id == call.id
    assert stored.role == TranscriptRole.user
    assert stored.text == "I need an appointment"
    assert stored.created_at is not None


# ── end_call ─────────────────────────────────────────────────────────────────
def test_end_call_sets_ended_at_and_status(db_session):
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")

    ended = end_call(db_session, call.id, CallStatus.completed)

    assert ended.ended_at is not None
    assert ended.status == CallStatus.completed


def test_end_call_unknown_id_returns_none(db_session):
    assert end_call(db_session, 99999, CallStatus.completed) is None


# ── Full lifecycle: start → turns → stop, driven straight against the DB ──────
def test_call_lifecycle(db_session):
    """No Twilio, no pipeline — just the same DB operations run_bot performs,
    called directly, with the status checked at each transition."""
    practice = add_practice(db_session)

    # start: in_progress, started, not yet ended
    call = start_call(db_session, practice.id, "+18135550000")
    assert call.status == CallStatus.in_progress
    assert call.started_at is not None
    assert call.ended_at is None

    # turns: recording what was said doesn't change the call's status
    add_transcript_turn(db_session, call.id, TranscriptRole.assistant, "Hi, how can I help?")
    add_transcript_turn(db_session, call.id, TranscriptRole.user, "I need an appointment")
    db_session.refresh(call)
    assert call.status == CallStatus.in_progress
    assert [t.role for t in call.transcript_turns] == [TranscriptRole.assistant, TranscriptRole.user]

    # stop: in_progress -> completed, ended_at stamped
    ended = end_call(db_session, call.id, CallStatus.completed)
    assert ended.status == CallStatus.completed
    assert ended.ended_at is not None
    assert ended.ended_at >= ended.started_at


def test_call_lifecycle_crash_path(db_session):
    """Same drive, but the pipeline crashes instead of stopping cleanly —
    the transition lands on failed, not completed."""
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")
    add_transcript_turn(db_session, call.id, TranscriptRole.user, "Hello?")

    ended = end_call(db_session, call.id, CallStatus.failed)

    assert ended.status == CallStatus.failed
    assert ended.ended_at is not None


# ── TranscriptObserver: least-invasive frame-level hook ────────────────────────
def push_frame(observer, source, frame):
    """Fake a FramePushed event the way BaseObserver.on_push_frame receives it —
    only .source and .frame are read, so the rest can stay unset."""
    data = SimpleNamespace(source=source, frame=frame)
    return asyncio.run(observer.on_push_frame(data))


def test_observer_saves_final_stt_transcript(monkeypatch):
    saved = []
    monkeypatch.setattr(
        bot_module, "_insert_transcript_turn",
        lambda call_id, role, text: saved.append((call_id, role, text)),
    )
    observer = bot_module.TranscriptObserver(call_id=42)
    stt = Mock(spec=STTService)

    push_frame(observer, stt, TranscriptionFrame(
        text="I need an appointment", user_id="u1", timestamp="2026-09-18T00:00:00Z",
    ))

    assert saved == [(42, TranscriptRole.user, "I need an appointment")]


def test_observer_ignores_transcript_from_non_stt_source(monkeypatch):
    saved = []
    monkeypatch.setattr(
        bot_module, "_insert_transcript_turn",
        lambda call_id, role, text: saved.append((call_id, role, text)),
    )
    observer = bot_module.TranscriptObserver(call_id=42)
    not_stt = Mock()  # a downstream hop re-pushing the same frame

    push_frame(observer, not_stt, TranscriptionFrame(
        text="I need an appointment", user_id="u1", timestamp="2026-09-18T00:00:00Z",
    ))

    assert saved == []


def test_observer_saves_completed_llm_response_as_one_turn(monkeypatch):
    saved = []
    monkeypatch.setattr(
        bot_module, "_insert_transcript_turn",
        lambda call_id, role, text: saved.append((call_id, role, text)),
    )
    observer = bot_module.TranscriptObserver(call_id=42)
    llm = Mock(spec=LLMService)

    push_frame(observer, llm, LLMFullResponseStartFrame())
    push_frame(observer, llm, LLMTextFrame(text="Hi there, "))
    push_frame(observer, llm, LLMTextFrame(text="how can I help?"))
    push_frame(observer, llm, LLMFullResponseEndFrame())

    assert saved == [(42, TranscriptRole.assistant, "Hi there, how can I help?")]


def test_observer_ignores_llm_text_from_non_llm_source(monkeypatch):
    saved = []
    monkeypatch.setattr(
        bot_module, "_insert_transcript_turn",
        lambda call_id, role, text: saved.append((call_id, role, text)),
    )
    observer = bot_module.TranscriptObserver(call_id=42)
    downstream = Mock()  # e.g. the TTS service re-pushing the same LLMTextFrame

    push_frame(observer, downstream, LLMFullResponseStartFrame())
    push_frame(observer, downstream, LLMTextFrame(text="echoed"))
    push_frame(observer, downstream, LLMFullResponseEndFrame())

    assert saved == []


def test_observer_skips_empty_llm_response(monkeypatch):
    saved = []
    monkeypatch.setattr(
        bot_module, "_insert_transcript_turn",
        lambda call_id, role, text: saved.append((call_id, role, text)),
    )
    observer = bot_module.TranscriptObserver(call_id=42)
    llm = Mock(spec=LLMService)

    push_frame(observer, llm, LLMFullResponseStartFrame())
    push_frame(observer, llm, LLMFullResponseEndFrame())

    assert saved == []


# ── run_bot: try/finally must end the call whichever way the pipeline exits ──
def _patch_bot_dependencies(monkeypatch, *, runner_run):
    """Replace every heavy Pipecat class run_bot constructs with a cheap fake,
    so the try/finally call-ending logic can be exercised without a real audio
    pipeline. `runner_run` is installed as WorkerRunner.run()."""

    for name in (
        "AnthropicLLMService", "DeepgramSTTService", "DeepgramTTSService",
        "SileroVADAnalyzer", "LLMContext", "LLMUserAggregatorParams",
    ):
        monkeypatch.setattr(bot_module, name, Mock())

    monkeypatch.setattr(
        bot_module, "LLMContextAggregatorPair", lambda context, **kw: (Mock(), Mock())
    )
    monkeypatch.setattr(bot_module, "Pipeline", lambda steps: Mock())

    class FakeEventEmitter:
        def event_handler(self, name):
            return lambda fn: fn

    class FakeAudioBuffer(FakeEventEmitter):
        async def start_recording(self):
            pass

    monkeypatch.setattr(bot_module, "AudioBufferProcessor", FakeAudioBuffer)

    class FakeWorker(FakeEventEmitter):
        def __init__(self, *a, **kw):
            pass

        async def queue_frames(self, frames):
            pass

    monkeypatch.setattr(bot_module, "PipelineWorker", FakeWorker)

    class FakeRunner:
        def __init__(self, *a, **kw):
            pass

        async def add_workers(self, worker):
            pass

        async def run(self):
            await runner_run()

        async def cancel(self):
            pass

    monkeypatch.setattr(bot_module, "WorkerRunner", FakeRunner)


def _fake_transport():
    transport = Mock()
    transport.event_handler = lambda name: (lambda fn: fn)
    return transport


def test_run_bot_marks_call_completed_when_pipeline_stops_normally(monkeypatch):
    ended = []
    monkeypatch.setattr(
        bot_module, "_end_call", lambda call_id, status: ended.append((call_id, status))
    )
    _patch_bot_dependencies(monkeypatch, runner_run=lambda: asyncio.sleep(0))

    call = bot_module.CallSession(call_id=42, practice_id=1, caller_number="+1", call_sid="CA1")
    runner_args = SimpleNamespace(pipeline_idle_timeout_secs=30, handle_sigint=False)

    asyncio.run(bot_module.run_bot(_fake_transport(), runner_args, call, testing=False))

    assert ended == [(42, CallStatus.completed)]


def test_run_bot_marks_call_failed_and_reraises_on_crash(monkeypatch):
    ended = []
    monkeypatch.setattr(
        bot_module, "_end_call", lambda call_id, status: ended.append((call_id, status))
    )

    async def boom():
        raise RuntimeError("pipeline exploded")

    _patch_bot_dependencies(monkeypatch, runner_run=boom)

    call = bot_module.CallSession(call_id=42, practice_id=1, caller_number="+1", call_sid="CA1")
    runner_args = SimpleNamespace(pipeline_idle_timeout_secs=30, handle_sigint=False)

    with pytest.raises(RuntimeError, match="pipeline exploded"):
        asyncio.run(bot_module.run_bot(_fake_transport(), runner_args, call, testing=False))

    # The DB write happened even though the pipeline crashed.
    assert ended == [(42, CallStatus.failed)]


# ── start_call_session: reads the stream params, carries the call id ──────────
def test_start_call_session_carries_call_id(monkeypatch):
    inserted = {}

    def fake_insert(practice_id, caller_number):
        inserted.update(practice_id=practice_id, caller_number=caller_number)
        return 42

    monkeypatch.setattr(bot_module, "_insert_call", fake_insert)
    runner_args = SimpleNamespace(call_data=SimpleNamespace(
        body={"practice_id": "7", "from_number": "+18135550000"},
        from_number="+18135550000",
        call_id="CA123",
    ))

    call = asyncio.run(bot_module.start_call_session(runner_args))

    assert inserted == {"practice_id": 7, "caller_number": "+18135550000"}
    assert call == bot_module.CallSession(
        call_id=42, practice_id=7, caller_number="+18135550000", call_sid="CA123",
    )


def test_start_call_session_requires_practice_id():
    runner_args = SimpleNamespace(call_data=SimpleNamespace(
        body={}, from_number="+18135550000", call_id="CA123",
    ))

    with pytest.raises(ValueError):
        asyncio.run(bot_module.start_call_session(runner_args))


# ── A real call, read back through the actual API surface ─────────────────────
def test_real_call_visible_via_api_with_turns(client, db_session):
    """Runs the same DB writes run_bot's pipeline performs for one call —
    start, turns, stop — then reads it back the way a client actually would:
    GET /calls?practice_id=... and GET /calls/{id}/transcript."""
    practice = add_practice(db_session)

    call = start_call(db_session, practice.id, "+18135550000")
    add_transcript_turn(db_session, call.id, TranscriptRole.assistant, "Hi, how can I help?")
    add_transcript_turn(db_session, call.id, TranscriptRole.user, "I need an appointment")
    end_call(db_session, call.id, CallStatus.completed)

    response = client.get("/calls", params={"practice_id": practice.id})
    assert response.status_code == 200
    [listed] = response.json()
    assert listed["id"] == call.id
    assert listed["status"] == "completed"
    assert listed["started_at"] is not None
    assert listed["ended_at"] is not None

    response = client.get(f"/calls/{call.id}/transcript")
    assert response.status_code == 200
    turns = response.json()
    assert [t["role"] for t in turns] == ["assistant", "user"]
    assert [t["text"] for t in turns] == ["Hi, how can I help?", "I need an appointment"]
    assert all(t["call_id"] == call.id for t in turns)


# ── /twilio/inbound passes practice + caller to the stream ────────────────────
def test_inbound_twiml_carries_stream_parameters(client):
    practice_id = client.post("/practices", json={
        "name": "Sunshine Dental", "timezone": "America/New_York", "phone": "+18135551234",
    }).json()["id"]

    response = client.post("/twilio/inbound", data={
        "From": "+18135550000", "To": "+18135551234", "CallSid": "CA123",
    })

    stream = ET.fromstring(response.content).find("Connect/Stream")
    params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
    assert params == {
        "practice_id": str(practice_id),
        "from_number": "+18135550000",
        "to_number": "+18135551234",
    }


def test_inbound_unknown_number_hangs_up(client):
    response = client.post("/twilio/inbound", data={
        "From": "+18135550000", "To": "+19999999999", "CallSid": "CA123",
    })

    root = ET.fromstring(response.content)
    assert root.find("Hangup") is not None
    assert root.find("Connect") is None


def test_inbound_number_no_longer_owned_gets_not_configured_and_no_call_row(client, db_session):
    """A number a practice used to own but no longer does — same code path a
    permanently unknown number takes, checked with a number that WAS valid a
    moment ago, plus the DB-side guarantee: no Call row gets created."""
    practice = add_practice(db_session, phone="+18135551234")
    practice.phone = "+18135559999"  # the practice moved off this number
    db_session.commit()

    response = client.post("/twilio/inbound", data={
        "From": "+18135550000", "To": "+18135551234", "CallSid": "CA123",
    })

    assert "This number is not configured." in response.text
    root = ET.fromstring(response.content)
    assert root.find("Hangup") is not None
    assert root.find("Connect") is None
    assert db_session.query(Call).count() == 0


# ── GET /calls/{call_id}/transcript ────────────────────────────────────────────
def test_get_transcript_returns_turns_in_order(client, db_session):
    # client and db_session share the same StaticPool SQLite connection
    practice = add_practice(db_session)
    call = start_call(db_session, practice.id, "+18135550000")
    add_transcript_turn(db_session, call.id, TranscriptRole.assistant, "Hi, how can I help?")
    add_transcript_turn(db_session, call.id, TranscriptRole.user, "I need an appointment")

    response = client.get(f"/calls/{call.id}/transcript")

    assert response.status_code == 200
    turns = response.json()
    assert [t["role"] for t in turns] == ["assistant", "user"]
    assert [t["text"] for t in turns] == ["Hi, how can I help?", "I need an appointment"]


def test_get_transcript_unknown_call_404s(client):
    response = client.get("/calls/99999/transcript")

    assert response.status_code == 404
