from pydantic import BaseModel, ConfigDict
from datetime import datetime
from app.models.models import CallStatus

class PracticeCreate(BaseModel):
    name: str
    timezone: str
    phone: str
    business_hours: dict | None = None

class PracticeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    timezone: str
    phone: str
    business_hours: dict | None = None
    created_at: datetime


class CallCreate(BaseModel):
    practice_id: int
    caller_number: str
    started_at: datetime
    ended_at: datetime  | None = None
    status: CallStatus
    recording_url:str | None = None


class CallRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    practice_id: int
    caller_number: str
    started_at: datetime
    ended_at: datetime | None = None
    status: CallStatus
    recording_url:str | None = None
    created_at: datetime 
