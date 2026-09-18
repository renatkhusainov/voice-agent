from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.models import Call, Practice, TranscriptTurn
from app.schemas.practice import (
    CallCreate,
    CallRead,
    PracticeCreate,
    PracticeRead,
    TranscriptTurnRead,
)


router_practices = APIRouter(prefix="/practices", tags=["practices"])
router_calls = APIRouter(prefix="/calls", tags=["calls"])


@router_practices.get("/{practice_id}", response_model=PracticeRead)
def get_practice(practice_id: int, db: Session = Depends(get_db)):
    practice = db.get(Practice, practice_id)
    if practice is None:
        raise HTTPException(status_code=404, detail="Practice not found")
    return practice


@router_practices.post("", response_model=PracticeRead, status_code=201)
def create_practice(payload: PracticeCreate, db: Session = Depends(get_db)):
    practice = Practice(**payload.model_dump())
    db.add(practice)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Practice with this phone already exists")
    db.refresh(practice)
    return practice


@router_practices.get("", response_model=list[PracticeRead])
def list_practices(
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
):
    stmt = select(Practice).order_by(Practice.id).offset(skip).limit(limit)
    return db.scalars(stmt).all()


@router_calls.get("", response_model=list[CallRead])
def get_calls(
    practice_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
):
    stmt = (
        select(Call)
        .where(Call.practice_id == practice_id)
        .order_by(Call.started_at.desc(), Call.id.desc())
        .offset(skip)
        .limit(limit)
    )
    return db.scalars(stmt).all()


@router_calls.post("", response_model=CallRead, status_code=201)
def create_call(payload: CallCreate, db: Session = Depends(get_db)):
    practice = db.get(Practice, payload.practice_id)
    if practice is None:
        raise HTTPException(status_code=404, detail="Practice not found")

    call = Call(**payload.model_dump())
    db.add(call)
    db.commit()
    db.refresh(call)
    return call


@router_calls.get("/{call_id}/transcript", response_model=list[TranscriptTurnRead])
def get_transcript(call_id: int, db: Session = Depends(get_db)):
    if db.get(Call, call_id) is None:
        raise HTTPException(status_code=404, detail="Call not found")

    stmt = (
        select(TranscriptTurn)
        .where(TranscriptTurn.call_id == call_id)
        .order_by(TranscriptTurn.created_at, TranscriptTurn.id)
    )
    return db.scalars(stmt).all()

