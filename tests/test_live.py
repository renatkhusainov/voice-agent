"""app/agent/live.py: the agent's tools on a live call, driven the way
Pipecat drives them (a FunctionCallParams per tool call, an LLMContext per
inference) without audio, Twilio, or a real LLM.

What's real: the tool handlers, the confirmation gate, DialogState in a
fakeredis store, the database (the test SQLite engine, written from worker
threads exactly as on a call), and the transcript writes. What's fake: the
LLM service, which only records the frames the handlers push into it.
"""

import asyncio
import time
from datetime import datetime, timezone

import fakeredis
from redis import exceptions as redis_exceptions
from pipecat.frames.frames import EndWorkerFrame, TTSSpeakFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.llm_service import FunctionCallParams

from app.agent import live, store
from app.agent.live import (
    FILLER_LINE,
    DialogStateLLMService,
    LiveCallAgent,
    count_caller_turns,
    state_key,
)
from app.agent.state import DialogIntent
from app.agent.tools import ESCALATION_MESSAGE, TOOLS
from app.models.models import Appointment, Intent, Lead, Practice, TranscriptRole, TranscriptTurn
from app.services.calls import start_call
from tests.conftest import TestingSessionLocal

# A Tuesday, 9 AM in New York (far enough ahead to stay in the future) — see the read-back assertion below.
SLOT = "2030-10-01T13:00:00Z"


class FakeLLM:
    """Stands in for the LLM service a handler receives in params.llm."""

    def __init__(self, events: list):
        self.events = events
        self.registered: dict[str, object] = {}

    async def push_frame(self, frame, direction=None):
        self.events.append(("frame", frame))

    def register_function(self, name, handler, **kwargs):
        self.registered[name] = handler


class Harness:
    """One live call: a practice, its Call row, and a LiveCallAgent for it."""

    def __init__(self, db_session, **agent_kwargs):
        self.practice = Practice(name="Sunshine Dental", timezone="America/New_York", phone="+18135551234")
        db_session.add(self.practice)
        db_session.commit()
        self.call = start_call(db_session, self.practice.id, "+18135550142")
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        agent_kwargs.setdefault("filler_enabled", False)
        self.agent = LiveCallAgent(
            call_id=self.call.id, practice_id=self.practice.id,
            session_factory=TestingSessionLocal, redis_client=self.redis, **agent_kwargs,
        )
        self.events: list = []
        self.llm = FakeLLM(self.events)
        self.messages: list[dict] = [{"role": "developer", "content": "Please introduce yourself to the user."}]

    def caller_says(self, text: str) -> LLMContext:
        """A new caller message, and the context the next inference sees."""
        self.messages.append({"role": "user", "content": text})
        return self.context()

    def context(self) -> LLMContext:
        return LLMContext(messages=list(self.messages))

    async def tool_call(self, name: str, arguments: dict, context: LLMContext | None = None):
        """Runs one tool call exactly as Pipecat would: build the params,
        call the handler, collect what it passes to result_callback."""
        results = []

        async def result_callback(result, *, properties=None):
            self.events.append(("result", result))
            results.append((result, properties))

        await self.agent.handle_tool_call(FunctionCallParams(
            function_name=name, tool_call_id="toolu_1", arguments=arguments, llm=self.llm,
            pipeline_worker=None, context=context or self.context(), result_callback=result_callback,
        ))
        [(result, properties)] = results
        return result, properties

    def state(self):
        return store.get_state(state_key(self.call.id), client=self.redis)


def booking_args(practice_id):
    return {
        "practice_id": practice_id, "caller_name": "Dana Lee", "callback_number": "+18135550142",
        "service": "cleaning", "requested_slot": SLOT,
    }


def run(coro):
    return asyncio.run(coro)


# ── Turn counting ──────────────────────────────────────────────────────────
def test_count_caller_turns_counts_only_what_the_caller_said():
    messages = [
        {"role": "developer", "content": "Please introduce yourself to the user."},
        {"role": "assistant", "content": "Hi, Sunshine Dental!"},
        {"role": "user", "content": "I need a cleaning"},
        {"role": "assistant", "content": [{"type": "tool_use"}]},
        {"role": "tool", "content": "{}"},
        {"role": "user", "content": "Yes"},
    ]

    assert count_caller_turns(messages) == 2


def test_repeated_inference_on_the_same_context_does_not_advance_the_turn(db_session):
    # Pipecat can run inference several times for one caller message (after
    # each tool result, or a discarded speculative run). The counter must not
    # move on those, or the gate could confirm a booking on its own.
    h = Harness(db_session)
    context = h.caller_says("Book me a cleaning")

    for _ in range(3):
        run(h.agent.system_prompt_for(context))

    assert h.state().turn_count == 1


# ── The confirmation gate on a live call ───────────────────────────────────
def test_booking_on_a_live_call_needs_a_new_caller_turn(db_session):
    h = Harness(db_session)
    args = booking_args(h.practice.id)

    # Turn 1: the caller asks; the model calls book_appointment -> proposal only.
    context = h.caller_says("Book me a cleaning Thursday at 9, Dana Lee, 813 555 0142")
    run(h.agent.system_prompt_for(context))
    result, _ = run(h.tool_call("book_appointment", args, context))

    assert result["status"] == "pending_confirmation"
    # Written to be said out loud: local time, digits one by one.
    assert result["read_back"] == "Dana Lee, phone number ending in 0 1 4 2, for a cleaning at 9 AM Tuesday the 1st"
    assert db_session.query(Appointment).count() == 0

    # Same turn, second call (e.g. the model retries after the tool result):
    # the caller hasn't said anything, so it's still not a confirmation.
    run(h.agent.system_prompt_for(context))
    result, _ = run(h.tool_call("book_appointment", args, context))
    assert result["status"] == "pending_confirmation"
    assert db_session.query(Appointment).count() == 0

    # Turn 2: the caller says yes -> now it books.
    h.messages.append({"role": "assistant", "content": "Dana Lee ... is that right?"})
    context = h.caller_says("Yes, that's right")
    run(h.agent.system_prompt_for(context))
    result, _ = run(h.tool_call("book_appointment", args, context))

    assert "appointment_id" in result
    assert result["spoken_time"] == "9 AM Tuesday the 1st"
    assert db_session.query(Appointment).count() == 1
    assert h.state().confirmed is True


def test_system_prompt_shows_the_current_booking_progress(db_session):
    h = Harness(db_session)
    context = h.caller_says("Book me a cleaning")
    before = run(h.agent.system_prompt_for(context))
    run(h.tool_call("book_appointment", booking_args(h.practice.id), context))

    after = run(h.agent.system_prompt_for(context))

    assert "CURRENT BOOKING PROGRESS" not in before
    assert "CURRENT BOOKING PROGRESS" in after
    assert "name ✓ (Dana Lee)" in after
    assert "Sunshine Dental" in after


def test_state_is_keyed_by_call_id_with_a_prefix(db_session):
    h = Harness(db_session)
    run(h.agent.system_prompt_for(h.caller_says("hello")))

    assert h.redis.exists(f"dialog_state:call:{h.call.id}")
    assert not h.redis.exists(f"dialog_state:{h.call.id}")


# ── Tool calls in the transcript ───────────────────────────────────────────
def test_each_tool_call_is_written_to_the_transcript_redacted(db_session):
    h = Harness(db_session)
    context = h.caller_says("Book me a cleaning")
    run(h.agent.system_prompt_for(context))
    run(h.tool_call("book_appointment", booking_args(h.practice.id), context))

    [turn] = db_session.query(TranscriptTurn).filter_by(call_id=h.call.id, role=TranscriptRole.tool).all()
    assert turn.text.startswith("book_appointment ")
    assert "pending_confirmation" in turn.text
    assert "5550142" not in turn.text  # redacted like every other turn


def test_tool_errors_go_back_to_the_model_as_an_error_result(db_session):
    h = Harness(db_session)

    result, _ = run(h.tool_call("book_appointment", {
        "practice_id": h.practice.id, "caller_name": "Dana Lee", "callback_number": "+18135550142",
        "service": "cleaning", "requested_slot": "call me at 813-555-0142",  # not a time
    }))

    # Names the field so the model can retry, and never echoes the value back.
    assert result["error"].startswith("Invalid input for this tool.")
    assert "requested_slot" in result["error"]
    assert "555" not in result["error"] and "Dana" not in result["error"]


def test_a_local_time_without_offset_books_the_practice_local_hour(db_session):
    # From a live run: the model sent "2026-12-15T09:30:00" for "9:30 in the
    # morning". No offset means the practice's local time (New York, EST).
    h = Harness(db_session)
    args = {**booking_args(h.practice.id), "requested_slot": "2030-01-15T09:30:00"}

    context = h.caller_says("Cleaning Tuesday the 15th at 9:30am please, Dana Lee, 813 555 0142")
    run(h.agent.system_prompt_for(context))
    proposal, _ = run(h.tool_call("book_appointment", args, context))
    context = h.caller_says("Yes")
    run(h.agent.system_prompt_for(context))
    # The model may now write the same time the other way, as the UTC `start`.
    booked, _ = run(h.tool_call("book_appointment", {**args, "requested_slot": "2030-01-15T14:30:00Z"}, context))

    assert proposal["read_back"].endswith("at 9:30 AM Tuesday the 15th")
    assert booked["spoken_time"] == "9:30 AM Tuesday the 15th"
    appointment = db_session.query(Appointment).one()
    assert appointment.requested_slot.replace(tzinfo=timezone.utc) == datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc)


# ── Escalation ─────────────────────────────────────────────────────────────
def test_escalation_speaks_the_handoff_line_and_ends_the_call(db_session):
    h = Harness(db_session)
    context = h.caller_says("My tooth is killing me and I'm bleeding")
    run(h.agent.system_prompt_for(context))

    result, properties = run(h.tool_call(
        "escalate_to_human", {"reason": "severe tooth pain and bleeding"}, context,
    ))

    # No LLM turn after the tool, so the model can't add advice of its own...
    assert properties.run_llm is False
    assert result["message_for_caller"] == ESCALATION_MESSAGE
    # ...the handoff line is spoken verbatim, then the call ends gracefully
    # (EndWorkerFrame, pushed after the line, so the audio goes out first).
    frames = [f for kind, f in h.events if kind == "frame"]
    assert [type(f) for f in frames] == [TTSSpeakFrame, EndWorkerFrame]
    assert frames[0].text == ESCALATION_MESSAGE

    lead = db_session.query(Lead).filter_by(call_id=h.call.id).one()
    assert lead.intent == Intent.callback
    assert lead.notes == "severe tooth pain and bleeding"
    assert h.state().intent == DialogIntent.escalate

    said = db_session.query(TranscriptTurn).filter_by(call_id=h.call.id, role=TranscriptRole.assistant).one()
    assert said.text == ESCALATION_MESSAGE


def test_escalation_drops_a_pending_booking(db_session):
    h = Harness(db_session)
    context = h.caller_says("Book me a cleaning")
    run(h.agent.system_prompt_for(context))
    run(h.tool_call("book_appointment", booking_args(h.practice.id), context))
    assert h.state().pending_confirmation is True

    run(h.tool_call("escalate_to_human", {"reason": "caller asked for a person"}, context))

    assert h.state().pending_confirmation is False


# ── Speak while working (filler) ───────────────────────────────────────────
def _slow_tools(monkeypatch, seconds):
    real = live.execute_tool

    def slow(*args, **kwargs):
        time.sleep(seconds)
        return real(*args, **kwargs)

    monkeypatch.setattr(live, "execute_tool", slow)


def test_filler_is_spoken_when_a_tool_is_slow(db_session, monkeypatch):
    _slow_tools(monkeypatch, 0.2)
    h = Harness(db_session, filler_enabled=True, filler_delay_secs=0.02)

    run(h.tool_call("answer_faq", {"question": "What are your hours?"}))

    # Filler first, then the tool's result, and never part of the LLM context
    # (it's said while a tool_use is still waiting for its tool_result).
    kinds = [kind for kind, _ in h.events]
    assert kinds == ["frame", "result"]
    [(_, filler)] = [e for e in h.events if e[0] == "frame"]
    assert isinstance(filler, TTSSpeakFrame)
    assert filler.text == FILLER_LINE
    assert filler.append_to_context is False
    assert h.agent.fillers_spoken == 1


def test_no_filler_before_an_escalation(db_session, monkeypatch):
    _slow_tools(monkeypatch, 0.1)
    h = Harness(db_session, filler_enabled=True, filler_delay_secs=0.01)

    run(h.tool_call("escalate_to_human", {"reason": "caller asked for a person"}))

    spoken = [f.text for kind, f in h.events if kind == "frame" and isinstance(f, TTSSpeakFrame)]
    assert spoken == [ESCALATION_MESSAGE]


def test_no_filler_when_the_tool_is_fast(db_session):
    h = Harness(db_session, filler_enabled=True, filler_delay_secs=5)

    run(h.tool_call("answer_faq", {"question": "What are your hours?"}))

    assert [kind for kind, _ in h.events] == ["result"]
    assert h.agent.fillers_spoken == 0


def test_no_filler_when_disabled_even_if_slow(db_session, monkeypatch):
    _slow_tools(monkeypatch, 0.1)
    h = Harness(db_session, filler_enabled=False, filler_delay_secs=0.01)

    run(h.tool_call("answer_faq", {"question": "What are your hours?"}))

    assert [kind for kind, _ in h.events] == ["result"]


# ── Wiring into Pipecat ────────────────────────────────────────────────────
def test_register_attaches_a_handler_for_every_advertised_tool(db_session):
    h = Harness(db_session)

    h.agent.register(h.llm)

    assert set(h.llm.registered) == {schema.name for schema in TOOLS.standard_tools}


def test_llm_service_rebuilds_the_system_prompt_before_each_inference(db_session, monkeypatch):
    h = Harness(db_session)
    seen_prompts = []

    async def fake_parent_process_context(self, context):
        seen_prompts.append(self._settings.system_instruction)

    monkeypatch.setattr(AnthropicLLMService, "_process_context", fake_parent_process_context)
    llm = DialogStateLLMService(
        agent=h.agent, api_key="test", settings=AnthropicLLMService.Settings(model="claude-haiku-4-5-20251001"),
    )

    context = h.caller_says("Book me a cleaning")
    run(llm._process_context(context))
    run(h.tool_call("book_appointment", booking_args(h.practice.id), context))
    run(llm._process_context(context))  # the inference after the tool result

    assert "Sunshine Dental" in seen_prompts[0]
    assert "CURRENT BOOKING PROGRESS" not in seen_prompts[0]
    assert "CURRENT BOOKING PROGRESS" in seen_prompts[1]


# ── Redis unreachable: the call keeps working, only booking is refused ──────
class DownRedis:
    """What the real client does when the host doesn't resolve — the exact
    failure from a live call (`Error 8 connecting to redis:6379`)."""

    def _fail(self, *args, **kwargs):
        raise redis_exceptions.ConnectionError("Error 8 connecting to redis:6379.")

    get = set = delete = exists = _fail


def test_prompt_still_builds_when_redis_is_down(db_session):
    h = Harness(db_session)
    h.agent._redis = DownRedis()

    prompt = run(h.agent.system_prompt_for(h.caller_says("Hi")))

    assert "Sunshine Dental" in prompt
    assert "CURRENT BOOKING PROGRESS" not in prompt


def test_escalation_still_works_when_redis_is_down(db_session):
    h = Harness(db_session)
    h.agent._redis = DownRedis()

    result, properties = run(h.tool_call("escalate_to_human", {"reason": "bleeding"}))

    assert result["message_for_caller"] == ESCALATION_MESSAGE
    assert properties.run_llm is False
    assert db_session.query(Lead).filter_by(call_id=h.call.id).one().intent == Intent.callback


def test_booking_is_refused_not_attempted_when_redis_is_down(db_session):
    h = Harness(db_session)
    h.agent._redis = DownRedis()

    for _ in range(2):  # even a "confirming" second call
        result, _ = run(h.tool_call("book_appointment", booking_args(h.practice.id)))
        assert result["error"].startswith("Booking is unavailable right now.")

    assert db_session.query(Appointment).count() == 0


# ── Barge-in during the confirming call ─────────────────────────────────────
def test_barge_in_during_booking_commits_once_and_the_retry_gets_the_booking(db_session, monkeypatch):
    h = Harness(db_session)
    args = booking_args(h.practice.id)
    context = h.caller_says("Book me a cleaning")
    run(h.agent.system_prompt_for(context))
    run(h.tool_call("book_appointment", args, context))  # proposal
    context = h.caller_says("Yes")
    run(h.agent.system_prompt_for(context))

    _slow_tools(monkeypatch, 0.2)

    async def interrupted_then_retried():
        # Pipecat cancels the running handler when the caller starts talking.
        confirming = asyncio.create_task(h.tool_call("book_appointment", args, context))
        await asyncio.sleep(0.05)
        confirming.cancel()
        try:
            await confirming
        except asyncio.CancelledError:
            pass
        # The handler only lets the cancellation through once its thread is
        # done: the booking is already committed and state saved here.
        assert db_session.query(Appointment).count() == 1
        assert h.state().confirmed is True
        # The model tries again after the interruption.
        return await h.tool_call("book_appointment", args, context)

    retry, _ = run(interrupted_then_retried())

    assert "appointment_id" in retry  # the booking itself, not a second read-back
    assert db_session.query(Appointment).count() == 1
