"""POST /agent/turn — text in, text out, conversation kept per session_id.
GET /agent/sessions/{id} — a dev-only window into that session's DialogState.

POST /turn is the harness every later module (evals, module 7+) tests the
agent against: a plain HTTP round trip through the same run_agent_loop and
TOOLS the real call path will eventually use (app/services/bot.py), without a
phone call, Twilio, or audio needed to exercise it.

`get_anthropic_client` and `get_redis_client` are their own dependencies, not
called inline, purely so a test can override them with fakes the way `get_db`
is already overridden for the test database — see tests/test_agent_api.py.
`get_redis_client` returning `None` (the default) means "use the real,
lazily-constructed client" — see app/agent/store.py; a test overrides it to
return a shared `fakeredis.FakeRedis()` instead.
"""

from anthropic import Anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import store
from app.agent.prompts import PROMPT_VERSION
from app.agent.session import get_or_create_session, take_turn
from app.agent.state import DialogState
from app.config import settings
from app.db import get_db
from app.models.models import Practice
from app.services.calls import start_call

router = APIRouter(prefix="/agent", tags=["agent"])


def get_anthropic_client() -> Anthropic:
    return Anthropic(api_key=settings.anthropic_api_key)


def get_redis_client():
    return None


class TurnRequest(BaseModel):
    session_id: str = Field(min_length=1, description="Caller-chosen id; the same id continues a conversation")
    practice_id: int
    message: str = Field(min_length=1)


class TurnResponse(BaseModel):
    session_id: str
    call_id: int
    reply: str
    prompt_version: str


@router.post("/turn", response_model=TurnResponse)
def agent_turn(
    payload: TurnRequest,
    db: Session = Depends(get_db),
    client: Anthropic = Depends(get_anthropic_client),
    redis_client=Depends(get_redis_client),
) -> TurnResponse:
    practice = db.get(Practice, payload.practice_id)
    if practice is None:
        raise HTTPException(status_code=404, detail="Practice not found")

    # caller_number="agent-harness" marks Call rows this text harness created,
    # distinct from a real caller's number, in case anyone goes looking later.
    session = get_or_create_session(
        payload.session_id, create_call_id=lambda: start_call(db, practice.id, "agent-harness").id,
    )
    reply = take_turn(
        client, db, session, practice=practice, message=payload.message, redis_client=redis_client,
    )
    return TurnResponse(session_id=payload.session_id, call_id=session.call_id, reply=reply, prompt_version=PROMPT_VERSION)


@router.get("/sessions/{id}", response_model=DialogState)
def get_session_state(id: str, redis_client=Depends(get_redis_client)) -> DialogState:
    """Dev-only: the raw DialogState for one session — intent, collected
    slots (unmasked), confirmation status, turn count. Exactly the internal
    bookkeeping app/agent/state.py's confirmation gate uses to decide whether
    book_appointment may execute; nothing here is redacted or summarized,
    which is the whole point of a debug endpoint and exactly why it's gated.

    Disabled (404, indistinguishable from a route that doesn't exist) unless
    ENVIRONMENT=development — see app/config.py.
    """
    if settings.environment != "development":
        raise HTTPException(status_code=404, detail="Not Found")

    state = store.get_state(id, client=redis_client)
    if state is None:
        raise HTTPException(status_code=404, detail=f"No session state for id={id!r}")
    return state
