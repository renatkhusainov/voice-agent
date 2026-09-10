from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.schemas.practice import PracticeRead, PracticeCreate, CallCreate, CallRead
from app.db import get_db
from app.models.models import Practice
from app.models.models import Call

from sqlalchemy import select


router_practices = APIRouter(prefix="/practices", tags=["practices"])
router_calls = APIRouter(prefix="/calls", tags=["calls"])

@router_practices.get("/{practice_id}", response_model=PracticeRead)
def get_practice(practice_id:int, db:Session = Depends(get_db)):
    practice = db.get(Practice, practice_id)
    if practice is None:
        raise HTTPException(status_code=404, detail="Practice not found")
    return practice

@router_practices.post("", response_model=PracticeRead, status_code=201)
def create_practice(payload:PracticeCreate, db:Session = Depends(get_db)):
    practice = Practice(**payload.model_dump())
    db.add(practice)
    db.commit()
    db.refresh(practice)
    return practice

@router_practices.get("", response_model=list[PracticeRead])
def list_practices(
    skip: int = 0,
    limit: int = Query(10, le=100),
    db: Session = Depends(get_db)):
        stmt = select(Practice).offset(skip).limit(limit)
        return db.scalars(stmt).all()

@router_calls.get("", response_model=list[CallRead])
def get_calls(
    practice_id: int,
    skip: int = 0,
    limit: int = Query(10, le=100),
    db: Session = Depends(get_db),
):
    stmt = select(Call).where(Call.practice_id == practice_id)
    return db.scalars(stmt.offset(skip).limit(limit)).all()


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


