"""A fake Anthropic client — no network, no key needed — shared by
test_loop.py (the loop's own mechanics) and test_agent_api.py (the /agent/turn
route, via a FastAPI dependency override of get_anthropic_client). One
implementation, so both suites fake the same API the same way.
"""

from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

__all__ = ["FakeClient", "FakeMessages", "text_response", "tool_use_response"]


def text_response(text: str, *, stop_reason: str = "end_turn") -> Message:
    return Message(
        id="msg_text", type="message", role="assistant", model="claude-haiku-4-5-20251001",
        content=[TextBlock(type="text", text=text, citations=None)],
        stop_reason=stop_reason, stop_sequence=None,
        usage=Usage(input_tokens=10, output_tokens=10),
    )


def tool_use_response(name: str, input_: dict, *, tool_use_id: str = "tu_1", also_text: str | None = None) -> Message:
    content = ([TextBlock(type="text", text=also_text, citations=None)] if also_text else []) + [
        ToolUseBlock(type="tool_use", id=tool_use_id, name=name, input=input_)
    ]
    return Message(
        id="msg_tool", type="message", role="assistant", model="claude-haiku-4-5-20251001",
        content=content, stop_reason="tool_use", stop_sequence=None,
        usage=Usage(input_tokens=10, output_tokens=10),
    )


class FakeMessages:
    """Stands in for client.messages: returns queued responses in order,
    records every call's kwargs so a test can inspect exactly what was sent."""

    def __init__(self, responses: list[Message]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        # run_agent_loop keeps appending to the same `messages` list across
        # iterations, so without snapshotting here every recorded call would
        # end up pointing at its final, fully-mutated state instead of what
        # was actually sent at call time.
        kwargs = {**kwargs, "messages": list(kwargs["messages"])}
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeMessages.create called more times than responses were queued")
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses: list[Message]):
        self.messages = FakeMessages(responses)
