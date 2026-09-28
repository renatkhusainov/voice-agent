"""Per-session conversation state shared by the two text-in/text-out front
ends to the agent: POST /agent/turn (app/routers/agent.py) and the
`python -m app.agent.chat` REPL. Both call get_or_create_session() then
take_turn() — everything about running the agent loop for one more message
lives here once, not duplicated between the HTTP route and the CLI.

Two different kinds of state live here, on purpose, with different
lifetimes:

  * AgentSession (this module) — the raw conversation (`messages`) and which
    Call row it points at. In-memory only, keyed by whatever session_id the
    caller picks. Restarting the process drops it. This is a test/dev
    harness for driving the agent without a phone call, not a second record
    of what was said — transcript_turns, written by the real call path in
    app/services/bot.py, is that.

  * DialogState (app/agent/state.py) — the state *machine's* record: intent,
    collected booking slots, confirmation status. Persisted in Redis
    (app/agent/store.py), keyed by session_id, TTL one hour. This *does*
    survive a process restart — see docs/notes/conversation-state-machine.md
    and the restart test in tests/test_store.py. So after a restart, the raw
    transcript is gone but the structured facts ("name: Dana Lee, service:
    cleaning") aren't — a deliberate recovery shape, not an inconsistency.

The one Call row a session's tools need to point at (book_appointment and
escalate_to_human both require a real call_id) is created once per session,
reused for every turn after, exactly like a real call gets one Call row for
its whole duration. It is never closed out by this harness (no hang-up event
exists here to close it on), so a harness session leaves its Call row at
status=in_progress permanently — acceptable for a dev tool, not something
that should be mistaken for a real call's lifecycle.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from anthropic import Anthropic
from sqlalchemy.orm import Session

from app.agent import store
from app.agent.loop import run_agent_loop
from app.agent.prompts import build_system_prompt
from app.agent.state import DialogState, build_gated_dispatch
from app.agent.tools import TOOLS
from app.models.models import Practice

if TYPE_CHECKING:
    from redis import Redis

__all__ = ["AgentSession", "DEFAULT_MODEL", "get_or_create_session", "reset_sessions", "take_turn"]

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


@dataclass
class AgentSession:
    session_id: str
    call_id: int
    messages: list[dict[str, Any]] = field(default_factory=list)


_SESSIONS: dict[str, AgentSession] = {}


def get_or_create_session(session_id: str, *, create_call_id: Callable[[], int]) -> AgentSession:
    """`create_call_id` runs only the first time `session_id` is seen — every
    later turn for the same session reuses that call_id and message history."""
    session = _SESSIONS.get(session_id)
    if session is None:
        session = AgentSession(session_id=session_id, call_id=create_call_id())
        _SESSIONS[session_id] = session
    return session


def reset_sessions() -> None:
    """Test-only: drop all in-memory session state so one test can't see
    another's sessions. Does not touch Redis — that's DialogState's own
    store, with its own lifetime; see app/agent/store.py."""
    _SESSIONS.clear()


def take_turn(
    client: Anthropic,
    db: Session,
    session: AgentSession,
    *,
    practice: Practice,
    message: str,
    model: str = DEFAULT_MODEL,
    redis_client: "Redis | None" = None,
) -> str:
    """Append `message` as a user turn, run the agent loop against a
    DialogState-aware system prompt and the state-gated dispatch
    (app/agent/state.py), persist the updated state, and return the
    assistant's reply text.

    `redis_client` is test-only plumbing — a fakeredis.FakeRedis() in tests,
    None (the real, lazily-constructed client) everywhere else. See
    app/agent/store.py.
    """
    state = store.get_state(session.session_id, client=redis_client) or DialogState()
    state.turn_count += 1

    system = build_system_prompt(practice, state=state)
    dispatch = build_gated_dispatch(session.call_id, state)

    session.messages.append({"role": "user", "content": message})
    result = run_agent_loop(
        client, db=db, model=model, system=system, messages=session.messages,
        tools_schema=TOOLS, dispatch=dispatch, call_id=session.call_id,
    )
    session.messages = result.messages

    store.set_state(session.session_id, state, client=redis_client)
    return result.final_text
