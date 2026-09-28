"""app/agent/loop.py: the tool-use loop, driven with a fake Anthropic client
(no network, no key needed — deterministic and fast) so the loop's own logic
— when to call a tool, when to stop, what happens on failure, what gets
logged — is what's under test, not the network.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from anthropic.types import Message, ToolUseBlock, Usage
from pipecat.adapters.services.anthropic_adapter import AnthropicLLMAdapter
from pydantic import BaseModel

from app.agent.loop import (
    GENERIC_TOOL_FAILURE_MESSAGE,
    MAX_ITERATIONS_FALLBACK_MESSAGE,
    run_agent_loop,
)
from app.agent.tools import TOOLS, ToolHandler, build_dispatch
from app.models.models import Practice
from app.services.calls import start_call
from tests.agent_fakes import FakeClient, text_response, tool_use_response


def base_messages():
    return [{"role": "user", "content": "Hi, I'd like to book a cleaning."}]


# ── No tool use: a plain reply ───────────────────────────────────────────────
def test_no_tool_use_returns_immediately(db_session):
    client = FakeClient([text_response("Sure, when works for you?")])

    result = run_agent_loop(
        client, db=db_session, model="claude-haiku-4-5-20251001", system="You are a front desk agent.",
        messages=base_messages(), tools_schema=TOOLS, dispatch=build_dispatch(call_id=1),
    )

    assert result.final_text == "Sure, when works for you?"
    assert result.iterations == 1
    assert result.stopped_reason == "end_turn"
    assert client.messages.calls[0]["model"] == "claude-haiku-4-5-20251001"
    assert client.messages.calls[0]["system"] == "You are a front desk agent."


def test_original_messages_list_is_not_mutated(db_session):
    client = FakeClient([text_response("hi")])
    original = base_messages()
    original_copy = list(original)

    run_agent_loop(
        client, db=db_session, model="m", system="s",
        messages=original, tools_schema=TOOLS, dispatch=build_dispatch(call_id=1),
    )

    assert original == original_copy


def test_tools_sent_are_the_real_generated_schema_not_hand_rolled(db_session):
    client = FakeClient([text_response("hi")])

    run_agent_loop(
        client, db=db_session, model="m", system="s",
        messages=base_messages(), tools_schema=TOOLS, dispatch=build_dispatch(call_id=1),
    )

    assert client.messages.calls[0]["tools"] == AnthropicLLMAdapter().to_provider_tools_format(TOOLS)


# ── One tool_use round trip, then a reply ────────────────────────────────────
def test_tool_use_then_end_turn_runs_the_tool_and_continues(db_session):
    client = FakeClient([
        tool_use_response("answer_faq", {"question": "Do you take Delta Dental?"}),
        text_response("Let me have the office confirm that for you."),
    ])

    result = run_agent_loop(
        client, db=db_session, model="m", system="s",
        messages=base_messages(), tools_schema=TOOLS, dispatch=build_dispatch(call_id=1),
    )

    assert result.iterations == 2
    assert result.stopped_reason == "end_turn"
    assert result.final_text == "Let me have the office confirm that for you."
    assert len(client.messages.calls) == 2

    # The second call's messages carry the tool_result from the first round.
    second_call_messages = client.messages.calls[1]["messages"]
    tool_result_turn = second_call_messages[-1]
    assert tool_result_turn["role"] == "user"
    [block] = tool_result_turn["content"]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "tu_1"
    assert block["is_error"] is False
    assert '"answered":false' in block["content"]


def test_result_messages_include_the_full_conversation(db_session):
    client = FakeClient([
        tool_use_response("answer_faq", {"question": "hours?"}),
        text_response("I'll check on that."),
    ])

    result = run_agent_loop(
        client, db=db_session, model="m", system="s",
        messages=base_messages(), tools_schema=TOOLS, dispatch=build_dispatch(call_id=1),
    )

    roles = [m["role"] for m in result.messages]
    assert roles == ["user", "assistant", "user", "assistant"]


# ── Failure modes: none of these should raise out of the loop ───────────────
def test_unknown_tool_name_is_reported_as_an_error_result_not_a_crash(db_session):
    client = FakeClient([
        tool_use_response("delete_the_database", {}),
        text_response("Never mind."),
    ])

    result = run_agent_loop(
        client, db=db_session, model="m", system="s",
        messages=base_messages(), tools_schema=TOOLS, dispatch=build_dispatch(call_id=1),
    )

    [block] = client.messages.calls[1]["messages"][-1]["content"]
    assert block["is_error"] is True
    assert "Unknown tool" in block["content"]
    assert result.iterations == 2  # the loop kept going


def test_tool_input_validation_failure_is_reported_not_raised(db_session):
    client = FakeClient([
        tool_use_response("answer_faq", {}),  # missing required "question"
        text_response("okay"),
    ])

    result = run_agent_loop(
        client, db=db_session, model="m", system="s",
        messages=base_messages(), tools_schema=TOOLS, dispatch=build_dispatch(call_id=1),
    )

    [block] = client.messages.calls[1]["messages"][-1]["content"]
    assert block["is_error"] is True
    assert "Invalid input" in block["content"]
    assert result.stopped_reason == "end_turn"


def test_tool_error_message_is_passed_back_to_the_model(db_session):
    client = FakeClient([
        tool_use_response("check_availability", {
            "practice_id": 99999,
            "date_range": {"start_date": "2026-10-01", "end_date": "2026-10-01"},
        }),
        text_response("okay"),
    ])

    run_agent_loop(
        client, db=db_session, model="m", system="s",
        messages=base_messages(), tools_schema=TOOLS, dispatch=build_dispatch(call_id=1),
    )

    [block] = client.messages.calls[1]["messages"][-1]["content"]
    assert block["is_error"] is True
    assert "No practice with id 99999" in block["content"]


def test_unexpected_exception_becomes_the_generic_message_not_leaked(db_session):
    def broken(db, payload):
        raise RuntimeError("leaking an internal stack detail")

    class EmptyInput(BaseModel):
        pass

    dispatch = {"broken_tool": ToolHandler(input_model=EmptyInput, call=broken)}
    client = FakeClient([
        tool_use_response("broken_tool", {}),
        text_response("okay"),
    ])

    run_agent_loop(
        client, db=db_session, model="m", system="s",
        messages=base_messages(), tools_schema=TOOLS, dispatch=dispatch,
    )

    [block] = client.messages.calls[1]["messages"][-1]["content"]
    assert block["is_error"] is True
    assert block["content"] == GENERIC_TOOL_FAILURE_MESSAGE
    assert "leaking" not in block["content"]
    assert "RuntimeError" not in block["content"]


def test_one_bad_tool_call_does_not_stop_the_loop(db_session):
    # The model asked for two tools in one turn; one fails, one succeeds —
    # both get a tool_result and the loop continues either way.
    response = Message(
        id="msg", type="message", role="assistant", model="m",
        content=[
            ToolUseBlock(type="tool_use", id="tu_bad", name="nope", input={}),
            ToolUseBlock(type="tool_use", id="tu_good", name="answer_faq", input={"question": "hi"}),
        ],
        stop_reason="tool_use", stop_sequence=None, usage=Usage(input_tokens=1, output_tokens=1),
    )
    client = FakeClient([response, text_response("done")])

    result = run_agent_loop(
        client, db=db_session, model="m", system="s",
        messages=base_messages(), tools_schema=TOOLS, dispatch=build_dispatch(call_id=1),
    )

    blocks = client.messages.calls[1]["messages"][-1]["content"]
    assert [b["tool_use_id"] for b in blocks] == ["tu_bad", "tu_good"]
    assert blocks[0]["is_error"] is True
    assert blocks[1]["is_error"] is False
    assert result.stopped_reason == "end_turn"


# ── max_iterations ────────────────────────────────────────────────────────
def test_max_iterations_stops_the_loop_and_uses_the_fallback_message(db_session):
    always_tool_use = [tool_use_response("answer_faq", {"question": "hi"}) for _ in range(5)]
    client = FakeClient(always_tool_use)

    result = run_agent_loop(
        client, db=db_session, model="m", system="s",
        messages=base_messages(), tools_schema=TOOLS, dispatch=build_dispatch(call_id=1),
        max_iterations=3,
    )

    assert result.iterations == 3
    assert result.stopped_reason == "max_iterations"
    assert result.final_text == MAX_ITERATIONS_FALLBACK_MESSAGE
    assert len(client.messages.calls) == 3  # never called a 4th time


def test_max_iterations_prefers_the_models_own_last_words_if_it_said_any(db_session):
    responses = [
        tool_use_response("answer_faq", {"question": "hi"}),
        tool_use_response("answer_faq", {"question": "hi"}, also_text="Still working on that..."),
    ]
    client = FakeClient(responses)

    result = run_agent_loop(
        client, db=db_session, model="m", system="s",
        messages=base_messages(), tools_schema=TOOLS, dispatch=build_dispatch(call_id=1),
        max_iterations=2,
    )

    assert result.stopped_reason == "max_iterations"
    assert result.final_text == "Still working on that..."


# ── Logging: every tool call logged, inputs/outputs redacted ────────────────
def test_tool_call_logs_are_redacted_and_a_real_call_uses_db_correctly(db_session, log_messages):
    practice = Practice(name="Sunshine Dental", timezone="America/New_York", phone="+18135551234")
    db_session.add(practice)
    db_session.commit()
    call = start_call(db_session, practice.id, "+18135550000")

    slot = datetime.now(timezone.utc).isoformat()
    client = FakeClient([
        tool_use_response("book_appointment", {
            "practice_id": practice.id, "caller_name": "Dana", "service": "cleaning",
            "callback_number": "+18135550142", "requested_slot": slot,
        }),
        text_response("Booked!"),
    ])

    run_agent_loop(
        client, db=db_session, model="m", system="s",
        messages=base_messages(), tools_schema=TOOLS, dispatch=build_dispatch(call_id=call.id),
        call_id=call.id,
    )

    logged = " ".join(log_messages)
    assert f"call={call.id}" in logged
    assert "tool=book_appointment" in logged
    assert "8135550142" not in logged
    assert "[PHONE]" in logged
    assert "Dana" in logged  # names are a documented, known gap — not silently over-claimed here


def test_tool_output_containing_phi_shaped_data_is_redacted_in_logs(db_session, log_messages):
    # book_appointment's result carries no PHI to redact (the phone number
    # lives on the *input*), so it can't tell us whether output= is actually
    # redacted or just happens to look clean. check_availability's result
    # does carry dated slots — real content for this to bite on.
    day = date.today() + timedelta(days=30)
    business_hours = {day.strftime("%a").lower(): ["09:00", "10:00"]}
    practice = Practice(name="Sunshine Dental", timezone="UTC", phone="+18135551234", business_hours=business_hours)
    db_session.add(practice)
    db_session.commit()

    client = FakeClient([
        tool_use_response("check_availability", {
            "practice_id": practice.id,
            "date_range": {"start_date": day.isoformat(), "end_date": day.isoformat()},
        }),
        text_response("Here are some times."),
    ])

    run_agent_loop(
        client, db=db_session, model="m", system="s",
        messages=base_messages(), tools_schema=TOOLS, dispatch=build_dispatch(call_id=1),
    )

    output_line = next(m for m in log_messages if "tool=check_availability output=" in m)
    assert day.isoformat() not in output_line
    assert "[DOB]" in output_line


def test_validation_failure_logs_no_raw_llm_input(db_session, log_messages):
    client = FakeClient([
        tool_use_response("check_availability", {"practice_id": "not-a-number", "date_range": {}}),
        text_response("okay"),
    ])

    run_agent_loop(
        client, db=db_session, model="m", system="s",
        messages=base_messages(), tools_schema=TOOLS, dispatch=build_dispatch(call_id=1),
    )

    logged = " ".join(log_messages)
    assert "input failed validation" in logged
    assert "not-a-number" not in logged
