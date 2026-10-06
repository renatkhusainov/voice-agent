from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.models.models import CallStatus, TranscriptRole


class PracticeCreate(BaseModel):
    name: str = Field(min_length=1)
    timezone: str = Field(min_length=1)
    phone: str = Field(min_length=1)
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
    caller_number: str = Field(min_length=1)
    # Timezone offset is required (e.g. "2026-09-15T10:00:00Z") — columns are timestamptz
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    status: CallStatus
    recording_url: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def check_ended_after_started(self):
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ended_at must not be before started_at")
        return self


class CallRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    practice_id: int
    caller_number: str
    started_at: datetime
    ended_at: datetime | None = None
    status: CallStatus
    recording_url: str | None = None
    created_at: datetime


class TranscriptTurnRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    call_id: int
    role: TranscriptRole
    text: str
    created_at: datetime
