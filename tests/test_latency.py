"""app/services/latency.py: per-stage timestamps for a caller turn.

Frames are fed to the real observer with pipeline-clock timestamps chosen
by the test, so every offset below is exact, not "roughly right".
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    EndFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService

from app.models.models import CallMetric, Practice
from app.services import latency as latency_module
from app.services.calls import start_call
from app.services.latency import LatencyObserver
from tests.conftest import TestingSessionLocal

MS = 1_000_000  # pipeline clock is in nanoseconds

STT, LLM, TTS, OUT = Mock(spec=STTService), Mock(spec=LLMService), Mock(spec=TTSService), Mock()


class Recorder:
    def __init__(self):
        self.saved = []

    def __call__(self, call_id, label, timings):
        self.saved.append((call_id, label, timings))


def feed(observer, events):
    """events: (ms_on_pipeline_clock, source, frame). Each frame is also
    re-pushed once more at a later hop, as it would be in a real pipeline,
    to prove it's only counted on first sighting."""
    async def go():
        for at_ms, source, frame in events:
            await observer.on_push_frame(SimpleNamespace(source=source, frame=frame, timestamp=at_ms * MS))
            await observer.on_push_frame(SimpleNamespace(source=Mock(), frame=frame, timestamp=(at_ms + 999) * MS))
    asyncio.run(go())


def a_tool_turn(start=10_000):
    """Caller stops talking at `start` ms; VAD (stop_secs=0.2) decides 200 ms later.

    Broadcast frames appear twice as *separate instances* (upstream and
    downstream copies), exactly as Pipecat sends them: the first live smoke
    run split every turn in two until the observer handled that."""
    return [
        (start - 2_000, OUT, UserStartedSpeakingFrame()),
        (start - 2_000, OUT, UserStartedSpeakingFrame()),
        (start + 200, OUT, VADUserStoppedSpeakingFrame(stop_secs=0.2, timestamp=1_790_000_000.2)),
        (start + 450, STT, TranscriptionFrame(text="book me in", user_id="u", timestamp="t")),
        (start + 520, OUT, UserStoppedSpeakingFrame()),
        (start + 521, OUT, UserStoppedSpeakingFrame()),
        (start + 540, LLM, LLMFullResponseStartFrame()),
        (start + 1_100, LLM, FunctionCallInProgressFrame(
            function_name="check_availability", tool_call_id="t1", arguments={})),
        (start + 1_101, LLM, FunctionCallInProgressFrame(
            function_name="check_availability", tool_call_id="t1", arguments={})),
        (start + 1_150, LLM, LLMFullResponseEndFrame()),
        (start + 1_180, LLM, FunctionCallResultFrame(
            function_name="check_availability", tool_call_id="t1", arguments={}, result={})),
        (start + 1_200, LLM, LLMFullResponseStartFrame()),
        (start + 1_900, LLM, LLMTextFrame(text="We have ")),
        (start + 2_300, TTS, TTSAudioRawFrame(audio=b"\x00" * 160, sample_rate=8000, num_channels=1)),
        (start + 2_340, OUT, BotStartedSpeakingFrame()),
        (start + 2_600, LLM, LLMFullResponseEndFrame()),
    ]


def test_every_stage_is_an_offset_from_when_the_caller_stopped_speaking():
    rec = Recorder()
    obs = LatencyObserver(7, "baseline", save=rec)

    feed(obs, a_tool_turn())
    asyncio.run(obs.flush())

    [(call_id, label, t)] = rec.saved
    assert (call_id, label, t.turn) == (7, "baseline", 1)
    assert t.vad_ms == 200
    assert t.stt_final_ms == 450
    assert t.turn_end_ms == 520
    assert t.llm_start_ms == 540
    assert t.llm_first_token_ms == 1_900   # the post-tool inference's first text
    assert t.llm_last_token_ms == 2_600
    assert t.tts_first_byte_ms == 2_300
    assert t.first_audio_out_ms == 2_340
    assert t.llm_inferences == 2
    assert t.tools == [{"name": "check_availability", "start_ms": 1_100, "end_ms": 1_180}]
    assert t.interrupted is False


def test_the_greeting_before_any_caller_speech_is_not_a_turn():
    rec = Recorder()
    obs = LatencyObserver(7, "baseline", save=rec)

    feed(obs, [
        (100, LLM, LLMFullResponseStartFrame()),
        (700, LLM, LLMTextFrame(text="Hi, Sunshine Dental!")),
        (1_100, OUT, BotStartedSpeakingFrame()),
    ])
    asyncio.run(obs.flush())

    assert rec.saved == []


def test_consecutive_turns_are_numbered_and_closed_by_the_next_one():
    rec = Recorder()
    obs = LatencyObserver(7, "baseline", save=rec)

    feed(obs, a_tool_turn(start=10_000) + a_tool_turn(start=20_000))
    assert [t.turn for _, _, t in rec.saved] == [1]  # turn 1 closed when the caller spoke again
    asyncio.run(obs.flush())

    assert [t.turn for _, _, t in rec.saved] == [1, 2]
    assert rec.saved[1][2].first_audio_out_ms == 2_340  # offsets restart from turn 2's own anchor


def test_a_turn_the_caller_interrupts_before_any_audio_is_marked_interrupted():
    rec = Recorder()
    obs = LatencyObserver(7, "baseline", save=rec)

    # The caller talks again while the first inference is still running.
    feed(obs, a_tool_turn()[:7] + [(11_000, OUT, UserStartedSpeakingFrame())])

    [(_, _, t)] = rec.saved
    assert t.interrupted is True
    assert t.first_audio_out_ms is None


def test_a_vad_stop_that_the_caller_talked_through_is_not_the_anchor():
    # "I'd like... [pause] ...a cleaning": the pause trips the VAD, but the
    # caller keeps going. Only the last VAD stop before the turn counts.
    rec = Recorder()
    obs = LatencyObserver(7, "baseline", save=rec)

    feed(obs, [(5_200, OUT, VADUserStoppedSpeakingFrame(stop_secs=0.2, timestamp=1.0))] + a_tool_turn())
    asyncio.run(obs.flush())

    assert rec.saved[0][2].turn_end_ms == 520  # anchored at 10_000, not 5_000


def test_end_of_call_closes_the_open_turn():
    rec = Recorder()
    obs = LatencyObserver(7, "baseline", save=rec)

    feed(obs, a_tool_turn() + [(13_000, OUT, EndFrame())])

    assert len(rec.saved) == 1


def test_a_failed_save_never_raises_into_the_call():
    def broken(*args):
        raise RuntimeError("db down")

    obs = LatencyObserver(7, "baseline", save=broken)
    feed(obs, a_tool_turn())
    asyncio.run(obs.flush())  # no exception


def test_default_save_writes_a_call_metrics_row(db_session, monkeypatch):
    monkeypatch.setattr(latency_module, "SessionLocal", TestingSessionLocal)
    practice = Practice(name="Sunshine Dental", timezone="America/New_York", phone="+18135551234")
    db_session.add(practice)
    db_session.commit()
    call = start_call(db_session, practice.id, "+18135550142")

    obs = LatencyObserver(call.id, "baseline")
    feed(obs, a_tool_turn())
    asyncio.run(obs.flush())

    row = db_session.query(CallMetric).filter_by(call_id=call.id).one()
    assert (row.turn, row.label, row.first_audio_out_ms, row.llm_inferences) == (1, "baseline", 2_340, 2)
    assert row.tools[0]["name"] == "check_availability"
