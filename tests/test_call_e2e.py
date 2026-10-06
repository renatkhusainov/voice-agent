"""End-to-end proof for the two runtime items in docs/hipaa-design.md's
Definition of Done: a call's stored transcript is masked, and nothing it logs
carries transcript text or the caller's number.

No Twilio, no real Deepgram/Anthropic connection — driven straight through the
real `run_bot`, with only the heavy Pipecat service/pipeline classes faked out
(the same technique tests/test_calls.py uses for its try/finally tests). What
is NOT faked, and so is exercised for real, is exactly what this test exists to
check: TranscriptObserver, add_transcript_turn's redact() call, the DB write
happening through `with SessionLocal()`, and the close-out in run_bot's
finally block.
"""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import Mock

from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService

from app.models.models import Call, CallStatus, Practice, TranscriptTurn
from app.services import bot as bot_module
from app.services.calls import start_call
from tests.conftest import TestingSessionLocal

CALLER_NUMBER = "+18135550142"
CALLER_SAID = "Hi, this is Dana, my callback number is (813) 555-0142"
ASSISTANT_CONFIRMED = "Got it, confirming eight one three five five five zero one four two."


def test_call_produces_masked_transcript_and_leaves_no_phi_in_logs(monkeypatch, db_session, log_messages):
    """Simulates one full call: connect, caller speaks their number, the
    assistant reads it back, the call ends normally. Then checks both halves
    of the deliverable against the real, independently-observable state: the
    row actually sitting in the database, and every log message actually
    emitted (captured before the scrubber runs, so this can't pass just
    because the scrubber caught something that shouldn't have been logged)."""

    # _insert_call/_insert_transcript_turn/_end_call each open `with
    # SessionLocal()` per write (see app/services/bot.py) — point that at this
    # test's own StaticPool engine so the writes land somewhere this test, and
    # the assertions below, can see.
    monkeypatch.setattr(bot_module, "SessionLocal", TestingSessionLocal)

    practice = Practice(name="Sunshine Dental", timezone="America/New_York", phone="+18135551234")
    db_session.add(practice)
    db_session.commit()
    call = start_call(db_session, practice.id, CALLER_NUMBER)

    # Everything below this line is what run_bot actually constructs — faked
    # only so the test doesn't need a real audio socket, LLM or TTS. Compare
    # against the real bodies in app/services/bot.py: same classes, same
    # keyword arguments, nothing about the pipeline shape is invented here.
    captured_observers = []
    for name in (
        "DialogStateLLMService", "DeepgramSTTService", "DeepgramTTSService", "FlushingDeepgramTTSService",
        "SileroVADAnalyzer", "LLMContext", "LLMUserAggregatorParams", "UserBotLatencyObserver",
    ):
        monkeypatch.setattr(bot_module, name, Mock())
    monkeypatch.setattr(bot_module, "LLMContextAggregatorPair", lambda context, **kw: (Mock(), Mock()))
    monkeypatch.setattr(bot_module, "Pipeline", lambda steps: Mock())

    class FakeAudioBuffer:
        def event_handler(self, name):
            return lambda fn: fn

        async def start_recording(self):
            pass

    monkeypatch.setattr(bot_module, "AudioBufferProcessor", FakeAudioBuffer)

    class FakeWorker:
        def __init__(self, *a, observers=None, **kw):
            captured_observers.extend(observers or [])

        def event_handler(self, name):
            return lambda fn: fn

        async def queue_frames(self, frames):
            pass

    monkeypatch.setattr(bot_module, "PipelineWorker", FakeWorker)

    async def simulated_audio():
        # Stands in for the STT/LLM frames a real call would push through the
        # pipeline. TranscriptObserver.on_push_frame is the real one — this is
        # the exact call it gets in production.
        transcript = next(o for o in captured_observers if isinstance(o, bot_module.TranscriptObserver))
        stt, llm = Mock(spec=STTService), Mock(spec=LLMService)

        async def push(source, frame):
            await transcript.on_push_frame(SimpleNamespace(source=source, frame=frame))

        await push(stt, TranscriptionFrame(text=CALLER_SAID, user_id="u", timestamp="2026-09-22T00:00:00Z"))
        await push(llm, LLMFullResponseStartFrame())
        await push(llm, LLMTextFrame(text=ASSISTANT_CONFIRMED))
        await push(llm, LLMFullResponseEndFrame())

    class FakeRunner:
        def __init__(self, *a, **kw):
            pass

        async def add_workers(self, worker):
            pass

        async def run(self):
            await simulated_audio()

        async def cancel(self):
            pass

    monkeypatch.setattr(bot_module, "WorkerRunner", FakeRunner)

    transport = Mock()
    transport.event_handler = lambda name: (lambda fn: fn)
    runner_args = SimpleNamespace(pipeline_idle_timeout_secs=30, handle_sigint=False)
    session_call = bot_module.CallSession(
        call_id=call.id, practice_id=practice.id, caller_number=CALLER_NUMBER, call_sid="CAtest123",
    )

    asyncio.run(bot_module.run_bot(transport, runner_args, session_call, testing=False))

    # ── 1. The stored transcript shows masked digits ───────────────────────
    db_session.expire_all()  # the writes above came through a different Session
    turns = (
        db_session.query(TranscriptTurn)
        .filter_by(call_id=call.id)
        .order_by(TranscriptTurn.id)
        .all()
    )
    assert [t.text for t in turns] == [
        "Hi, this is Dana, my callback number is [PHONE]",
        "Got it, confirming [NUMBER].",
    ]
    stored = " ".join(t.text for t in turns)
    assert not re.search(r"\d", stored), f"a digit survived redaction: {stored!r}"
    assert "5550142" not in stored

    # The call itself closed out correctly (this is the piece run_bot's
    # try/finally guarantees — proven end-to-end here, not just in isolation).
    ended = db_session.get(Call, call.id)
    db_session.refresh(ended)
    assert ended.status == CallStatus.completed
    assert ended.ended_at is not None

    # ── 2. Log output from the call contains no transcript text and no
    #        caller number ────────────────────────────────────────────────
    logged = " ".join(log_messages)
    assert "5550142" not in logged
    assert "813" not in logged
    assert CALLER_NUMBER not in logged
    assert "Dana" not in logged
    assert "callback number" not in logged
    assert "confirming" not in logged
    # What SHOULD be there: the call is identifiable by id, just not by content.
    assert str(call.id) in logged
