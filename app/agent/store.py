"""Redis-backed DialogState persistence — first appearance of Redis in this
project (see docker-compose.yaml's `redis` service). Plain get/set/delete,
JSON-serialized Pydantic; nothing more.

Keyed by call_id for a real call once app/services/bot.py wires this in, or
by session_id in the text-mode harness (app/agent/session.py passes its own
session_id here) — the task's own framing: "State keyed by call_id (session
id in text mode)." This module doesn't care which; it just takes a string.

TTL: one hour (TTL_SECONDS). A conversation idle longer than that has its
state expire — matches how a real call can't sit open indefinitely either,
and keeps an abandoned dev-harness session from lingering in Redis forever.

No graceful fallback *here* if Redis is unreachable: get/set let a
connection error propagate rather than silently continuing with fresh,
empty state, so each caller decides explicitly. The text harness lets it
fail the request. A live call (app/agent/live.py) refuses booking, which is
what the gate protects, and keeps the rest of the call working. Silently
continuing with fresh state *here* would have turned the gate back into a
prompt hope without anyone deciding that.
"""

from typing import TYPE_CHECKING

import redis

from app.agent.state import DialogState
from app.config import settings

if TYPE_CHECKING:
    from redis import Redis

__all__ = ["TTL_SECONDS", "delete_state", "get_client", "get_state", "set_state"]

TTL_SECONDS = 60 * 60
KEY_PREFIX = "dialog_state:"

_client: "Redis | None" = None


def get_client() -> "Redis":
    """A lazily-constructed, process-wide client — built once, from
    settings.redis_url, on first use. Tests never touch this: they pass
    their own `client` (typically a fakeredis.FakeRedis()) to get_state/
    set_state/delete_state directly instead."""
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def get_state(key: str, *, client: "Redis | None" = None) -> DialogState | None:
    client = client or get_client()
    raw = client.get(KEY_PREFIX + key)
    if raw is None:
        return None
    return DialogState.model_validate_json(raw)


def set_state(key: str, state: DialogState, *, client: "Redis | None" = None) -> None:
    client = client or get_client()
    client.set(KEY_PREFIX + key, state.model_dump_json(), ex=TTL_SECONDS)


def delete_state(key: str, *, client: "Redis | None" = None) -> None:
    """Test/dev convenience — nothing in the normal turn-taking path calls
    this; state is left to expire via TTL_SECONDS instead."""
    client = client or get_client()
    client.delete(KEY_PREFIX + key)
