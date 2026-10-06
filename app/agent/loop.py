"""The tool-use loop against Anthropic's Messages API.

Send `messages` + `tools`; if the response's `stop_reason` is `"tool_use"`,
run the requested tool(s) from app/agent/tools.py, append their results as
`tool_result` blocks, and call the API again — repeat until the model stops
asking for a tool (`stop_reason` becomes something else, usually
`"end_turn"`) or `max_iterations` is hit.

The tool list sent to Anthropic is never hand-written: `ToolsSchema` (built in
app/agent/tools.py from each tool's Pydantic model via schema_from_model) is
converted to Anthropic's `input_schema` shape by Pipecat's own
`AnthropicLLMAdapter` — the same adapter `AnthropicLLMService` uses in
app/services/bot.py. Model -> FunctionSchema -> Anthropic tool dict, with
nothing describing a tool's parameters a second time anywhere in that chain.

`ToolHandler` and the dispatch table itself (`build_dispatch`) live in
app/agent/tools.py, not here — this module only ever calls a tool by name
through the mapping it's given, and stays ignorant of which of the five
tools need a call_id bound in and which don't.

Every tool call is logged — name, redacted input, and either a redacted
output or the failure — regardless of whether it succeeded, was rejected
(`ToolError`), failed validation, or crashed. Logging uses the same
app/phi/redact.py boundary the rest of the app writes transcripts through,
with the same known gaps (e.g. a caller's name isn't masked) — this is not a
stronger guarantee than that boundary already makes elsewhere.

This loop drives the text harness (POST /agent/turn, app.agent.chat). A live
call doesn't use it: there, Pipecat's AnthropicLLMService runs the loop itself
and calls each tool through app/agent/live.py — which goes through the same
`execute_tool` below, so the two paths can't drift apart on validation,
errors or logging.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic
from anthropic.types import Message
from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.adapters.services.anthropic_adapter import AnthropicLLMAdapter
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.agent.tools import ToolError, ToolHandler
from app.phi.redact import redact

__all__ = ["AgentLoopResult", "ToolOutcome", "execute_tool", "run_agent_loop", "DEFAULT_MAX_ITERATIONS"]

DEFAULT_MAX_ITERATIONS = 8
MAX_TOKENS = 1024

# Two distinct failure messages sent back to the model in a tool_result:
#  - a ToolError's own message, which is written to be caller-safe ("no such
#    practice") — the model can react to it, even relay it.
#  - this generic one, for anything else (a bug), so an unexpected exception
#    never hands the model (or, downstream, the caller) an internal detail.
GENERIC_TOOL_FAILURE_MESSAGE = "Something went wrong performing this action."

# What the caller hears if the loop is cut off mid tool-use, with nothing the
# model already said that could stand on its own as a reply.
MAX_ITERATIONS_FALLBACK_MESSAGE = "I'm having trouble finishing that — I'll have the office follow up with you."


@dataclass
class AgentLoopResult:
    """messages is the full conversation, tool turns included — pass it back
    in as the next call's `messages` to continue the conversation."""

    messages: list[dict[str, Any]]
    final_text: str
    iterations: int
    stopped_reason: str  # e.g. "end_turn", "max_tokens", or "max_iterations"


def _tool_result_block(tool_use_id: str, content: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content, "is_error": is_error}


@dataclass(frozen=True)
class ToolOutcome:
    """What one tool call produced, in a form either front end can use:
    `content` is the JSON output on success, or a caller-safe error message.
    `result` is the tool's own Pydantic result (None on any failure), for a
    caller that needs to act on it — app/agent/live.py ends the call after a
    successful escalate_to_human, for instance."""

    content: str
    is_error: bool
    result: BaseModel | None = None


def execute_tool(
    db: Session, name: str, tool_input: Any, dispatch: Mapping[str, ToolHandler], call_id: int | None
) -> ToolOutcome:
    """Validate and run one tool call by name. Shared by this module's loop
    (text harness) and app/agent/live.py (a live call through Pipecat), so
    validation, error handling and redacted logging are identical in both.
    Never raises: any failure (unknown tool, bad input, a ToolError, or a
    bare exception) becomes an error outcome instead, so one bad tool call
    can't take the whole loop — or the call — down with it.
    """
    handler = dispatch.get(name)
    if handler is None:
        logger.warning("call={} tool={} is not a known tool", call_id, name)
        return ToolOutcome(f"Unknown tool: {name}", is_error=True)

    try:
        payload = handler.input_model.model_validate(tool_input)
    except ValidationError as exc:
        logger.warning("call={} tool={} input failed validation ({} errors)", call_id, name, exc.error_count())
        return ToolOutcome(_validation_feedback(exc), is_error=True)

    logger.info("call={} tool={} input={}", call_id, name, redact(payload.model_dump_json()))

    try:
        result = handler.call(db, payload)
    except ToolError as exc:
        logger.info("call={} tool={} rejected: {}", call_id, name, redact(str(exc)))
        return ToolOutcome(str(exc), is_error=True)
    except Exception as exc:
        # A bug in a tool, not a normal "can't do that" outcome (that's
        # ToolError) — logged with the exception type only, matching how
        # TranscriptObserver._save handles a failed write in app/services/bot.py:
        # never the exception's own message, which could embed anything.
        logger.error("call={} tool={} crashed: {}", call_id, name, type(exc).__name__)
        return ToolOutcome(GENERIC_TOOL_FAILURE_MESSAGE, is_error=True)

    output_json = result.model_dump_json()
    logger.info("call={} tool={} output={}", call_id, name, redact(output_json))
    return ToolOutcome(output_json, is_error=False, result=result)


def _validation_feedback(exc: ValidationError) -> str:
    """Tells the model which fields to fix, so it can retry instead of giving
    up (a live call hit exactly that with a bare "invalid input"). Built from
    each error's field path and message only, never its `input`, which is the
    caller's own data. Not logged; the log line above records only the count.
    """
    problems = "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'input'}: {error['msg']}" for error in exc.errors()
    )
    return f"Invalid input for this tool. Fix and call it again — {problems}"


def _run_one_tool(
    db: Session, block: Any, dispatch: Mapping[str, ToolHandler], call_id: int | None
) -> dict[str, Any]:
    """One tool_use block -> its tool_result block. See execute_tool."""
    outcome = execute_tool(db, block.name, block.input, dispatch, call_id)
    return _tool_result_block(block.id, outcome.content, is_error=outcome.is_error)


def _text_from_response(response: Message) -> str:
    return "".join(block.text for block in response.content if block.type == "text")


def _text_from_message_dict(message: dict[str, Any] | None) -> str:
    if message is None:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") for block in content or [] if isinstance(block, dict) and block.get("type") == "text"
    )


def run_agent_loop(
    client: Anthropic,
    *,
    db: Session,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    tools_schema: ToolsSchema,
    dispatch: Mapping[str, ToolHandler],
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    call_id: int | None = None,
) -> AgentLoopResult:
    """Run the tool-use loop to completion (or to `max_iterations`).

    `messages` is the conversation so far (typically ending in a user turn);
    it is not mutated — the returned `AgentLoopResult.messages` is a new list
    with every assistant/tool_result turn this call added appended to it.
    """
    tools = AnthropicLLMAdapter().to_provider_tools_format(tools_schema)
    conversation = list(messages)

    for iteration in range(1, max_iterations + 1):
        response = client.messages.create(
            model=model, max_tokens=MAX_TOKENS, system=system, messages=conversation, tools=tools,
        )
        conversation.append({"role": "assistant", "content": [block.model_dump() for block in response.content]})

        if response.stop_reason != "tool_use":
            final_text = _text_from_response(response)
            logger.info(
                "call={} agent loop finished: iterations={} stop_reason={}",
                call_id, iteration, response.stop_reason,
            )
            return AgentLoopResult(
                messages=conversation, final_text=final_text, iterations=iteration,
                stopped_reason=response.stop_reason,
            )

        tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
        results = [_run_one_tool(db, block, dispatch, call_id) for block in tool_use_blocks]
        conversation.append({"role": "user", "content": results})

    logger.warning("call={} agent loop hit max_iterations={} still requesting tools", call_id, max_iterations)
    last_assistant = next((m for m in reversed(conversation) if m["role"] == "assistant"), None)
    final_text = _text_from_message_dict(last_assistant) or MAX_ITERATIONS_FALLBACK_MESSAGE
    return AgentLoopResult(
        messages=conversation, final_text=final_text, iterations=max_iterations, stopped_reason="max_iterations",
    )
