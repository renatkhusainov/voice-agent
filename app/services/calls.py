from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.models import Call, CallStatus, TranscriptRole, TranscriptTurn
from app.phi import redact


def start_call(db: Session, practice_id: int, caller_number: str) -> Call:
    """Insert an in-progress Call for a call that has just connected."""
    call = Call(
        practice_id=practice_id,
        caller_number=caller_number,
        started_at=datetime.now(timezone.utc),
        status=CallStatus.in_progress,
    )
    db.add(call)
    db.commit()
    db.refresh(call)
    return call


def add_transcript_turn(db: Session, call_id: int, role: TranscriptRole, text: str) -> TranscriptTurn:
    """Append one turn to a call's transcript.

    Called once per finalized user/assistant turn while the call is live, so
    each call inserts its own row and turns for different calls never contend.

    This is the PHI boundary: `text` is redacted here, on the way in, so the raw
    words are never stored no matter which caller (observer, test, script) got
    here. The LLM still hears the raw text; only what we persist is masked.
    """
    turn = TranscriptTurn(call_id=call_id, role=role, text=redact(text))
    db.add(turn)
    db.commit()
    db.refresh(turn)
    return turn


def end_call(db: Session, call_id: int, status: CallStatus) -> Call | None:
    """Close out a call: stamp ended_at and its final status.

    status is CallStatus.completed for a normal stop/disconnect, or
    CallStatus.failed when the pipeline crashed. Returns None if the call
    row doesn't exist (shouldn't happen — every call is inserted by start_call
    before the pipeline runs).
    """
    call = db.get(Call, call_id)
    if call is None:
        return None
    call.ended_at = datetime.now(timezone.utc)
    call.status = status
    db.commit()
    db.refresh(call)
    return call
