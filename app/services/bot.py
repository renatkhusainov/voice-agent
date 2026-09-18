import asyncio
import datetime
import io
import os
import wave
from dataclasses import dataclass

import aiofiles
from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner

from app.db import SessionLocal
from app.models.models import CallStatus, TranscriptRole
from app.services.calls import add_transcript_turn, end_call, start_call

# Pipecat's Twilio serializer reads TWILIO_* straight from os.environ, so .env must
# be exported. No override: variables already set (Docker, tests) take precedence.
load_dotenv()

RECORDINGS_DIR = "app/temp/recordings"


@dataclass(frozen=True)
class CallSession:
    """Per-call state shared by the pipeline and its event handlers."""

    call_id: int
    practice_id: int
    caller_number: str
    call_sid: str | None


async def save_audio(audio: bytes, sample_rate: int, num_channels: int, call_id: int):
    if len(audio) > 0:
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        filename = f"recording_{call_id}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
        filepath = os.path.join(RECORDINGS_DIR, filename)

        with io.BytesIO() as buffer:
            with wave.open(buffer, "wb") as wf:
                wf.setsampwidth(2)
                wf.setnchannels(num_channels)
                wf.setframerate(sample_rate)
                wf.writeframes(audio)
            async with aiofiles.open(filepath, "wb") as file:
                await file.write(buffer.getvalue())

        logger.info(f"Merged audio saved to {filepath}")
    else:
        logger.info("No audio data to save")


def _insert_call(practice_id: int, caller_number: str) -> int:
    with SessionLocal() as db:
        return start_call(db, practice_id, caller_number).id


def _insert_transcript_turn(call_id: int, role: TranscriptRole, text: str) -> None:
    with SessionLocal() as db:
        add_transcript_turn(db, call_id, role, text)


def _end_call(call_id: int, status: CallStatus) -> None:
    with SessionLocal() as db:
        end_call(db, call_id, status)


class TranscriptObserver(BaseObserver):
    """Persists each final STT transcript and each completed LLM response as a turn.

    An observer watches frames flow past every processor in the pipeline
    without being inserted into it — the least invasive way to capture what
    was said, vs. wiring into the context aggregators or adding a processor
    of our own. A frame is re-pushed at every hop it crosses (stt -> aggregator
    -> llm -> tts -> ...), so each frame type is only handled where it
    originates (`source` is the STT/LLM service itself); every other hop is a
    downstream echo of the same frame and is ignored, so each turn is only
    written once. LLM responses stream as a burst of LLMTextFrame chunks
    between LLMFullResponseStartFrame/EndFrame, so those are buffered and
    written as one turn on the End frame — matching "each completed response".
    """

    def __init__(self, call_id: int):
        super().__init__()
        self._call_id = call_id
        self._response_chunks: list[str] = []

    async def on_push_frame(self, data: FramePushed):
        src = data.source
        frame = data.frame

        if isinstance(src, STTService) and isinstance(frame, TranscriptionFrame):
            await self._save(TranscriptRole.user, frame.text)
        elif isinstance(src, LLMService):
            if isinstance(frame, LLMFullResponseStartFrame):
                self._response_chunks = []
            elif isinstance(frame, LLMTextFrame):
                self._response_chunks.append(frame.text)
            elif isinstance(frame, LLMFullResponseEndFrame):
                text = "".join(self._response_chunks)
                self._response_chunks = []
                await self._save(TranscriptRole.assistant, text)

    async def _save(self, role: TranscriptRole, text: str) -> None:
        if not text:
            return
        await asyncio.to_thread(_insert_transcript_turn, self._call_id, role, text)


async def start_call_session(runner_args: RunnerArguments) -> CallSession:
    """Record the call in the DB as soon as the Twilio stream starts.

    practice_id / from_number come from the <Parameter> elements that
    /twilio/inbound puts in the TwiML <Stream>.
    """
    call_data = runner_args.call_data
    practice_id = call_data.body.get("practice_id") if call_data else None
    if not practice_id:
        raise ValueError("Twilio stream is missing the practice_id parameter")

    caller_number = call_data.from_number or "unknown"
    # Sync SQLAlchemy — run it off the event loop so audio isn't blocked
    call_id = await asyncio.to_thread(_insert_call, int(practice_id), caller_number)

    return CallSession(
        call_id=call_id,
        practice_id=int(practice_id),
        caller_number=caller_number,
        call_sid=call_data.call_id,
    )


async def run_bot(
    transport: BaseTransport,
    runner_args: RunnerArguments,
    call: CallSession,
    testing: bool,
):
    # Tracks how the call ended, written in the finally block below regardless
    # of which path gets us there (normal stop, disconnect, or a crash) — so
    # the Call row never lingers as "in_progress" once the pipeline has exited.
    call_failed = False

    try:
        llm = AnthropicLLMService(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        settings=AnthropicLLMService.Settings(
            model="claude-haiku-4-5-20251001",
            system_instruction=(
                f"You are the front desk at iHeartSmiles Pediatric Dentistry. "
                "Answer in under two sentences. "
                "Never diagnose. "
                "For anything clinical say: I'll have the office call you back."
            ),
        ),
    )

        stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))

        tts = DeepgramTTSService(
            api_key=os.getenv("DEEPGRAM_API_KEY"),
            settings=DeepgramTTSService.Settings(
                voice="aura-asteria-en",
                model="aura-2.0",
            ),
        )

        context = LLMContext()
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(),
            ),
        )

        # NOTE: Watch out! This will save all the conversation in memory. You can
        # pass `buffer_size` to get periodic callbacks.
        audiobuffer = AudioBufferProcessor()

        pipeline = Pipeline(
            [
                transport.input(),  # Websocket input from client
                stt,  # Speech-To-Text
                user_aggregator,
                llm,  # LLM
                tts,  # Text-To-Speech
                transport.output(),  # Websocket output to client
                audiobuffer,  # Used to buffer the audio in the pipeline
                assistant_aggregator,
            ]
        )

        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=8000,
                audio_out_sample_rate=8000,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
            # Transcript persistence hangs off the pipeline as an observer — see
            # TranscriptObserver — rather than a processor or aggregator hook.
            observers=[TranscriptObserver(call.call_id)],
        )

        # We use `handle_sigint=False` because `uvicorn` is controlling keyboard
        # interruptions. We use `force_gc=True` to force garbage collection after
        # the runner finishes running a task which could be useful for long running
        # applications with multiple clients connecting.
        runner = WorkerRunner(handle_sigint=runner_args.handle_sigint, force_gc=True)
        await runner.add_workers(worker)

        @worker.event_handler("on_pipeline_error")
        async def on_pipeline_error(worker, frame):
            nonlocal call_failed
            call_failed = True
            logger.error(f"Call {call.call_id} pipeline error: {frame}")

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            # Start recording.
            await audiobuffer.start_recording()

            # Kick off the conversation.
            # "developer" is converted to a user turn by the Anthropic adapter;
            # custom roles are sent as-is and rejected by the API.
            context.add_message(
                {"role": "developer", "content": "Please introduce yourself to the user."}
            )
            await worker.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            await runner.cancel()

        @audiobuffer.event_handler("on_audio_data")
        async def on_audio_data(buffer, audio, sample_rate, num_channels):
            await save_audio(audio, sample_rate, num_channels, call.call_id)

        # Returns normally both on a clean end and on the cancel() triggered by
        # on_client_disconnected above — a disconnect is a normal call ending.
        await runner.run()
    except Exception:
        call_failed = True
        logger.exception(f"Call {call.call_id} pipeline crashed")
        raise
    finally:
        status = CallStatus.failed if call_failed else CallStatus.completed
        await asyncio.to_thread(_end_call, call.call_id, status)
        logger.info(f"Call {call.call_id} ended: status={status.value}")


async def bot(runner_args: RunnerArguments, testing: bool | None = False):
    """Main bot entry point compatible with Pipecat Cloud."""

    transport_params = {
        "twilio": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    }

    # create_transport auto-detects the telephony provider, builds the matching
    # serializer (here the TwilioFrameSerializer, using TWILIO_ACCOUNT_SID /
    # TWILIO_AUTH_TOKEN), and sets add_wav_header=False, so the bot only supplies
    # the params it cares about. It also fills runner_args.call_data.
    transport = await create_transport(runner_args, transport_params)

    call = await start_call_session(runner_args)
    logger.info(
        f"Call {call.call_id} started: practice={call.practice_id} "
        f"from={call.caller_number} sid={call.call_sid}"
    )

    await run_bot(transport, runner_args, call, testing)
