"""The agent's tools on a live phone call: the glue between Pipecat's
AnthropicLLMService (app/services/bot.py) and the same tools, confirmation
gate and DialogState the text harness uses (app/agent/session.py).

On a call, Pipecat runs the tool-use loop itself — app/agent/loop.py's
`run_agent_loop` is only for the text harness. What this module adds:

  * Tool handlers. Every tool in tools.TOOLS is registered with the LLM
    service. A handler runs the call through loop.execute_tool (the same
    validation, errors and redacted logging as the text harness) against
    state.build_gated_dispatch (the same confirmation gate). The DB and Redis
    work is synchronous, so it runs in a thread (`asyncio.to_thread`) — never
    on the event loop that's streaming audio.

  * DialogState per call. Keyed `call:<call_id>` in Redis (store.py). Before
    every inference, DialogStateLLMService asks `system_prompt_for()` for a
    prompt with the current booking progress — "the pipeline reads state
    each turn to enrich the system prompt."

  * The turn counter the gate depends on. In text mode, take_turn() adds one
    per inbound message. Here it's *derived*: the number of caller messages in
    the LLM context. Derived, not incremented, because Pipecat can run
    inference more than once for the same caller message (re-inference after
    a tool result, a speculative run that gets discarded). Counting would
    move the counter without the caller saying anything, which would let
    book_appointment confirm itself. Counting messages always gives the same
    answer for the same context.

  * Speak while working. If a tool is still running after
    settings.tool_filler_delay_ms, say FILLER_LINE so the caller doesn't
    hear dead air.

  * Escalation ends the call. After a successful escalate_to_human, the
    handoff line (tools.ESCALATION_MESSAGE) is spoken verbatim, not
    paraphrased by the model, and the call ends gracefully: EndWorkerFrame
    flushes the queued audio before the pipeline stops.

  * Redis down is not a silent call. Without DialogState the agent keeps
    talking (the prompt just lacks booking progress), and availability, FAQ
    and escalation still work. Only booking is refused, and the model is told
    to offer a callback instead.

  * Tool calls in the transcript. Each tool call is written to
    transcript_turns as role=tool (redacted, like every turn), so a
    transcript shows why the agent said what it said.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from loguru import logger
from pipecat.frames.frames import EndWorkerFrame, FunctionCallResultProperties, TTSSpeakFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.llm_service import FunctionCallParams, LLMService
from pipecat.services.settings import LLMSettings
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.agent import store
from app.agent.loop import ToolOutcome, execute_tool
from app.agent.prompts import build_system_prompt
from app.agent.state import DialogState, build_gated_dispatch
from app.agent.tools import TOOLS, EscalateToHumanResult
from app.db import SessionLocal
from app.models.models import Practice, TranscriptRole
from app.services.calls import add_transcript_turn

if TYPE_CHECKING:
    from redis import Redis

__all__ = [
    "FILLER_LINE",
    "DialogStateLLMService",
    "LiveCallAgent",
    "count_caller_turns",
    "state_key",
]

FILLER_LINE = "Let me check that for you."
# No filler before a handoff: the handoff line is the answer, and "Let me
# check that for you" right before "I'm sorry you're dealing with that"
# sounded wrong in a live run.
NO_FILLER_TOOLS = frozenset({"escalate_to_human"})

# Tools that can't run without DialogState: the confirmation gate lives there.
GATED_TOOLS = frozenset({"book_appointment"})
BOOKING_UNAVAILABLE = (
    "Booking is unavailable right now. Don't retry. Tell the caller you can't book "
    "it on this call, and offer to have the office call them back (escalate_to_human)."
)


def state_key(call_id: int) -> str:
    """The Redis key for a live call's DialogState. Prefixed so it can't
    collide with a text-mode session_id that happens to be a number."""
    return f"call:{call_id}"


def count_caller_turns(messages: Iterable[Any]) -> int:
    """How many times the caller has spoken: the "user" messages in the LLM
    context. Tool results are role "tool" and the kickoff instruction is
    role "developer", so neither counts.
    """
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user")


class LiveCallAgent:
    """Everything tool-related for one live call. Built once per call in
    app/services/bot.py; `register()` attaches it to that call's LLM service."""

    def __init__(
        self,
        *,
        call_id: int,
        practice_id: int,
        session_factory: Callable[[], Session] = SessionLocal,
        redis_client: "Redis | None" = None,
        filler_enabled: bool = True,
        filler_delay_secs: float = 0.3,
    ):
        self.call_id = call_id
        self.practice_id = practice_id
        self._session_factory = session_factory
        self._redis = redis_client  # None -> store.py's real client; fakeredis in tests
        self._filler_enabled = filler_enabled
        self._filler_delay_secs = filler_delay_secs
        # One tool call or prompt build at a time touches this call's state.
        # Pipecat runs parallel tool calls concurrently, and a
        # load-modify-save on the same Redis key must not interleave.
        self._lock = asyncio.Lock()
        self._practice: Practice | None = None

        # What gets reported when the call ends (see bot.py): counts and
        # timings only, never what was said.
        self.tool_calls = 0
        self.fillers_spoken = 0
        self.tool_durations_ms: list[int] = []

    @property
    def state_key(self) -> str:
        return state_key(self.call_id)

    def register(self, llm: LLMService) -> None:
        for schema in TOOLS.standard_tools:
            llm.register_function(schema.name, self.handle_tool_call)

    # ── Each inference: state -> system prompt ─────────────────────────────
    async def system_prompt_for(self, context: LLMContext) -> str:
        caller_turns = count_caller_turns(context.get_messages())
        async with self._lock:
            return await asyncio.to_thread(self._sync_turns_and_build_prompt, caller_turns)

    def _sync_turns_and_build_prompt(self, caller_turns: int) -> str:
        try:
            state = store.get_state(self.state_key, client=self._redis) or DialogState()
            # max(), not assignment: the counter never goes backwards, even if
            # Pipecat ever trims old messages out of the context.
            if caller_turns > state.turn_count:
                state.turn_count = caller_turns
                store.set_state(self.state_key, state, client=self._redis)
        except RedisError as exc:
            # Keep talking without the booking-progress section rather than
            # going silent on the caller. Booking itself still fails closed;
            # see _run_tool_blocking.
            logger.error("call={} DialogState unavailable ({}): prompt built without it", self.call_id, type(exc).__name__)
            state = None
        return build_system_prompt(self._load_practice(), state=state)

    def _load_practice(self) -> Practice:
        # Loaded once per call. Its name and id don't change mid-call, and the
        # loaded attributes stay readable after the session closes.
        if self._practice is None:
            with self._session_factory() as db:
                practice = db.get(Practice, self.practice_id)
            if practice is None:
                raise RuntimeError(f"Call {self.call_id}: practice {self.practice_id} not found")
            self._practice = practice
        return self._practice

    # ── Each tool call ─────────────────────────────────────────────────────
    async def handle_tool_call(self, params: FunctionCallParams) -> None:
        name = params.function_name
        started = time.perf_counter()
        filler = (
            FillerTimer(params.llm, self._filler_delay_secs, on_spoken=self._on_filler_spoken)
            if self._filler_enabled and name not in NO_FILLER_TOOLS else None
        )
        try:
            async with self._lock:
                outcome = await asyncio.to_thread(self._run_tool_blocking, name, dict(params.arguments))
        finally:
            if filler:
                await filler.stop()

        took_ms = round((time.perf_counter() - started) * 1000)
        self.tool_calls += 1
        self.tool_durations_ms.append(took_ms)
        logger.info(
            "call={} tool={} took_ms={} filler_spoken={} error={}",
            self.call_id, name, took_ms, bool(filler and filler.spoke), outcome.is_error,
        )

        result = {"error": outcome.content} if outcome.is_error else json.loads(outcome.content)

        if isinstance(outcome.result, EscalateToHumanResult):
            # No LLM turn after this: the handoff line comes from the tool, so
            # the model can't add advice of its own. Then hang up gracefully.
            await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=False))
            await self._speak_and_end(params.llm, outcome.result.message_for_caller)
            return

        await params.result_callback(result)

    def _run_tool_blocking(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Runs in a worker thread: load state, run the tool through the gate,
        save state, write the tool turn. Short-lived DB session, as everywhere
        else on the call path (see bot.py's _insert_transcript_turn).

        If Redis is unreachable, booking fails closed and everything else
        keeps working: book_appointment returns BOOKING_UNAVAILABLE (the model
        is told to offer a callback), while availability, FAQ and above all
        escalation run against a throwaway state. A throwaway state could
        never confirm a booking anyway (it has no pending proposal), but
        refusing outright spares the caller a read-back that can't lead
        anywhere.
        """
        try:
            state = store.get_state(self.state_key, client=self._redis) or DialogState()
            persist = True
        except RedisError as exc:
            logger.error("call={} tool={} DialogState unavailable ({})", self.call_id, name, type(exc).__name__)
            state, persist = DialogState(), False

        if not persist and name in GATED_TOOLS:
            outcome = ToolOutcome(BOOKING_UNAVAILABLE, is_error=True)
        else:
            dispatch = build_gated_dispatch(self.call_id, state)
            with self._session_factory() as db:
                outcome = execute_tool(db, name, arguments, dispatch, self.call_id)
            if persist:
                try:
                    store.set_state(self.state_key, state, client=self._redis)
                except RedisError as exc:
                    # Safe to carry on. A lost proposal means the next attempt
                    # re-proposes; a lost "confirmed" means a repeat attempt hits
                    # book_appointment's own "already booked" check.
                    logger.error("call={} tool={} DialogState not saved ({})", self.call_id, name, type(exc).__name__)

        self._save_turn(TranscriptRole.tool, f"{name} {json.dumps(arguments, default=str)} -> {outcome.content}")
        return outcome

    async def _on_filler_spoken(self) -> None:
        self.fillers_spoken += 1
        await asyncio.to_thread(self._save_turn, TranscriptRole.assistant, FILLER_LINE)

    def _save_turn(self, role: TranscriptRole, text: str) -> None:
        # Same rule as TranscriptObserver._save in bot.py: a lost transcript
        # row must not break the call, and the log line must not contain the
        # turn itself.
        try:
            with self._session_factory() as db:
                add_transcript_turn(db, self.call_id, role, text)
        except Exception as exc:
            logger.error("Call {} could not save a {} turn ({})", self.call_id, role.value, type(exc).__name__)

    async def _speak_and_end(self, llm: LLMService, line: str) -> None:
        await asyncio.to_thread(self._save_turn, TranscriptRole.assistant, line)
        await llm.push_frame(TTSSpeakFrame(line))
        # Pushed downstream after the line, so it only reaches the end of the
        # pipeline once the line's audio is ahead of it.
        await llm.push_frame(EndWorkerFrame(reason="escalated to human"))
        logger.info("call={} escalated: handoff line queued, ending call", self.call_id)


class FillerTimer:
    """Says FILLER_LINE once if the tool is still running after `delay_secs`.
    `stop()` cancels it if the tool finished first."""

    def __init__(self, llm: LLMService, delay_secs: float, *, on_spoken: Callable[[], Awaitable[None]]):
        self._llm = llm
        self._delay_secs = delay_secs
        self._on_spoken = on_spoken
        self.spoke = False
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        await asyncio.sleep(self._delay_secs)
        # append_to_context=False: this is said while a tool_use is waiting
        # for its tool_result. An assistant message between the two would be
        # an invalid request to Anthropic.
        await self._llm.push_frame(TTSSpeakFrame(FILLER_LINE, append_to_context=False))
        self.spoke = True
        await self._on_spoken()

    async def stop(self) -> None:
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


class DialogStateLLMService(AnthropicLLMService):
    """AnthropicLLMService that rebuilds its system prompt from DialogState
    before every inference, including the one after a tool result, so the
    model always sees current booking progress."""

    def __init__(self, *, agent: LiveCallAgent, **kwargs):
        super().__init__(**kwargs)
        self._agent = agent

    async def _process_context(self, context: LLMContext):
        prompt = await self._agent.system_prompt_for(context)
        # Only logs (and recomposes) when the prompt actually changed.
        await self._update_settings(LLMSettings(system_instruction=prompt))
        await super()._process_context(context)
