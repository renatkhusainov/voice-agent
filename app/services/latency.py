"""Where every millisecond of a caller turn goes: per-stage timestamps,
written to `call_metrics` (one row per turn) and to one structured log line.

The anchor for a turn is when the caller actually stopped speaking. The VAD
only *decides* that `stop_secs` of silence later, so the anchor is the VAD
frame's push time minus `stop_secs`. Everything else is an offset from it:

    speech end ─┬─ vad_ms            VAD decides speech ended (= stop_secs)
                ├─ stt_final_ms      last final transcript from Deepgram
                ├─ turn_end_ms       turn released (Smart Turn + transcript wait)
                ├─ llm_start_ms      first inference starts
                ├─ llm_first_token_ms  first text token (any inference in the turn)
                ├─ tools             [{name, start_ms, end_ms}]
                ├─ llm_last_token_ms last inference ends
                ├─ tts_first_byte_ms first TTS audio chunk
                └─ first_audio_out_ms  output transport starts sending audio

Timestamps are FramePushed.timestamp: the pipeline clock at the moment a
processor pushed the frame. Observers run asynchronously, so reading the
clock here would add queueing delay; the push timestamp doesn't. A frame is
seen once per hop it crosses, so each frame is counted only the first time
its id shows up. Some frames are *broadcast* (a separate instance sent
upstream and downstream): UserStarted/StoppedSpeakingFrame and the
function-call frames. Those are deduplicated by meaning instead: one turn per
caller utterance, one tool entry per tool_call_id.

A turn opens on UserStoppedSpeakingFrame (the pipeline has decided the caller
is done) and closes when the next turn starts or the call ends. So the row
also covers a tool turn's second inference and whatever the bot said after
the first audio. The greeting (no caller speech before it) isn't a turn.
"""

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    CancelFrame,
    EndFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import LLMUsageMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService

from app.db import SessionLocal
from app.models.models import CallMetric

__all__ = ["LatencyObserver", "TurnTimings"]

NS_PER_MS = 1_000_000


@dataclass
class TurnTimings:
    turn: int
    speech_end_at: datetime | None
    vad_ms: int | None = None
    stt_final_ms: int | None = None
    turn_end_ms: int | None = None
    llm_start_ms: int | None = None
    llm_first_token_ms: int | None = None
    llm_last_token_ms: int | None = None
    tts_first_byte_ms: int | None = None
    first_audio_out_ms: int | None = None
    llm_inferences: int = 0
    tools: list[dict[str, Any]] = field(default_factory=list)
    interrupted: bool = False


def _save_metric(call_id: int, label: str, timings: TurnTimings) -> None:
    with SessionLocal() as db:
        db.add(CallMetric(call_id=call_id, label=label, **asdict(timings)))
        db.commit()


class LatencyObserver(BaseObserver):
    def __init__(
        self,
        call_id: int,
        label: str,
        *,
        save: Callable[[int, str, TurnTimings], None] = _save_metric,
    ):
        super().__init__()
        self._call_id = call_id
        self._label = label
        self._save = save
        self._seen_ids: set[int] = set()
        self._seen_order: deque[int] = deque()

        # Candidates for the *next* turn, reset when the caller starts talking.
        self._last_vad: tuple[int, float, float] | None = None  # (anchor_ns, stop_secs, wall)
        self._last_stt_ns: int | None = None

        # True between UserStartedSpeakingFrame and the first matching stop;
        # the broadcast's second UserStoppedSpeakingFrame then finds it False.
        self._awaiting_stop = False
        self._turn: TurnTimings | None = None
        self._anchor_ns = 0
        self._open_tools: dict[str, dict[str, Any]] = {}
        self._tool_ids: set[str] = set()
        self._turn_count = 0
        self.turns: list[TurnTimings] = []  # closed turns, for the end-of-call summary

    def _first_sighting(self, frame_id: int) -> bool:
        if frame_id in self._seen_ids:
            return False
        self._seen_ids.add(frame_id)
        self._seen_order.append(frame_id)
        if len(self._seen_order) > 2000:
            self._seen_ids.discard(self._seen_order.popleft())
        return True

    def _ms(self, ts: int) -> int:
        return round((ts - self._anchor_ns) / NS_PER_MS)

    async def on_push_frame(self, data: FramePushed):
        frame, src, ts = data.frame, data.source, data.timestamp
        if not self._first_sighting(frame.id):
            return
        turn = self._turn

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            anchor = ts - round(frame.stop_secs * 1e9)
            self._last_vad = (anchor, frame.stop_secs, frame.timestamp - frame.stop_secs)
        elif isinstance(frame, UserStartedSpeakingFrame):
            if not self._awaiting_stop:
                await self._close_turn()
                self._last_vad, self._last_stt_ns = None, None
                self._awaiting_stop = True
        elif isinstance(frame, TranscriptionFrame) and isinstance(src, STTService):
            if turn is not None and turn.stt_final_ms is None:
                turn.stt_final_ms = self._ms(ts)  # final arrived after the turn was released
            elif turn is None:
                self._last_stt_ns = ts
        elif isinstance(frame, UserStoppedSpeakingFrame):
            if self._awaiting_stop:
                self._awaiting_stop = False
                await self._close_turn()
                self._open_turn(ts)
        elif turn is None:
            return
        elif isinstance(frame, LLMFullResponseStartFrame) and isinstance(src, LLMService):
            turn.llm_inferences += 1
            if turn.llm_start_ms is None:
                turn.llm_start_ms = self._ms(ts)
        elif isinstance(frame, LLMTextFrame) and isinstance(src, LLMService):
            if turn.llm_first_token_ms is None:
                turn.llm_first_token_ms = self._ms(ts)
        elif isinstance(frame, LLMFullResponseEndFrame) and isinstance(src, LLMService):
            turn.llm_last_token_ms = self._ms(ts)
        elif isinstance(frame, FunctionCallInProgressFrame):
            if frame.tool_call_id in self._tool_ids:
                return
            self._tool_ids.add(frame.tool_call_id)
            tool = {"name": frame.function_name, "start_ms": self._ms(ts), "end_ms": None}
            self._open_tools[frame.tool_call_id] = tool
            turn.tools.append(tool)
        elif isinstance(frame, FunctionCallResultFrame):
            tool = self._open_tools.pop(frame.tool_call_id, None)
            if tool is not None:
                tool["end_ms"] = self._ms(ts)
        elif isinstance(frame, TTSAudioRawFrame) and isinstance(src, TTSService):
            if turn.tts_first_byte_ms is None:
                turn.tts_first_byte_ms = self._ms(ts)
        elif isinstance(frame, BotStartedSpeakingFrame):
            if turn.first_audio_out_ms is None:
                turn.first_audio_out_ms = self._ms(ts)
        elif isinstance(frame, MetricsFrame) and isinstance(src, LLMService):
            # Token counts per inference, including the prompt-cache counters:
            # the evidence for whether caching actually engaged. Caution:
            # Pipecat 1.10's Anthropic service adds input_tokens from both
            # message_start and message_delta, so prompt_tokens here is about
            # 2x the real prompt (measured; see docs/notes/latency-budget.md).
            for data in frame.data:
                if isinstance(data, LLMUsageMetricsData):
                    u = data.value
                    logger.info(
                        "llm_usage call={} turn={} label={} prompt_tokens={} cache_read={} cache_write={}",
                        self._call_id, turn.turn, self._label, u.prompt_tokens,
                        u.cache_read_input_tokens, u.cache_creation_input_tokens,
                    )
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self._close_turn()

    def _open_turn(self, ts: int) -> None:
        self._turn_count += 1
        if self._last_vad is not None:
            self._anchor_ns, stop_secs, wall = self._last_vad
            vad_ms = round(stop_secs * 1000)
            speech_end_at = datetime.fromtimestamp(wall, tz=timezone.utc)
        else:
            # No VAD stop seen (e.g. a transcript-driven turn): anchor at the
            # turn decision itself, so offsets are still internally consistent.
            self._anchor_ns, vad_ms = ts, None
            speech_end_at = datetime.now(timezone.utc)
        self._turn = TurnTimings(turn=self._turn_count, speech_end_at=speech_end_at, vad_ms=vad_ms)
        self._turn.turn_end_ms = self._ms(ts)
        if self._last_stt_ns is not None:
            self._turn.stt_final_ms = self._ms(self._last_stt_ns)
        self._last_vad, self._last_stt_ns = None, None
        self._open_tools = {}
        self._tool_ids = set()

    async def _close_turn(self) -> None:
        turn, self._turn = self._turn, None
        if turn is None:
            return
        turn.interrupted = turn.first_audio_out_ms is None
        self.turns.append(turn)
        logger.info(
            "latency call={} turn={} label={} e2e_ms={} vad={} stt={} turn_end={} llm_start={} "
            "llm_first={} llm_last={} tts_first={} audio_out={} inferences={} tools={} interrupted={}",
            self._call_id, turn.turn, self._label, turn.first_audio_out_ms, turn.vad_ms, turn.stt_final_ms,
            turn.turn_end_ms, turn.llm_start_ms, turn.llm_first_token_ms, turn.llm_last_token_ms,
            turn.tts_first_byte_ms, turn.first_audio_out_ms, turn.llm_inferences,
            [(t["name"], t["start_ms"], t["end_ms"]) for t in turn.tools], turn.interrupted,
        )
        try:
            await asyncio.to_thread(self._save, self._call_id, self._label, turn)
        except Exception as exc:
            # Metrics must never take a call down; log the type only, as elsewhere.
            logger.error("Call {} could not save latency for turn {} ({})", self._call_id, turn.turn, type(exc).__name__)

    async def flush(self) -> None:
        """Close the turn still open when the call ends (run_bot's finally)."""
        await self._close_turn()
