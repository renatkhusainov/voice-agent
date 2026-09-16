from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text,
    DateTime, ForeignKey, Enum, JSON
)
from sqlalchemy.orm import DeclarativeBase, relationship
import enum


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Enums ────────────────────────────────────────────────
class CallStatus(str, enum.Enum):
    in_progress = "in_progress"
    completed   = "completed"
    missed      = "missed"
    failed      = "failed"


class Intent(str, enum.Enum):
    appointment = "appointment"
    question    = "question"
    callback    = "callback"
    other       = "other"


class AppointmentStatus(str, enum.Enum):
    pending     = "pending"
    confirmed   = "confirmed"
    cancelled   = "cancelled"
    rescheduled = "rescheduled"


# ── Models ───────────────────────────────────────────────
class Practice(Base):
    __tablename__ = "practices"

    id             = Column(Integer, primary_key=True)
    name           = Column(String, nullable=False)
    timezone       = Column(String, nullable=False)
    phone          = Column(String, nullable=False, unique=True)
    business_hours = Column(JSON, nullable=True)
    created_at     = Column(DateTime(timezone=True), default=utcnow)

    calls = relationship("Call", back_populates="practice")


class Call(Base):
    __tablename__ = "calls"

    id             = Column(Integer, primary_key=True)
    practice_id    = Column(Integer, ForeignKey("practices.id"), nullable=False)
    caller_number  = Column(String, nullable=False)
    started_at     = Column(DateTime(timezone=True), nullable=False)
    ended_at       = Column(DateTime(timezone=True), nullable=True)
    status         = Column(Enum(CallStatus), nullable=False)
    recording_url  = Column(String(500), nullable=True)
    created_at     = Column(DateTime(timezone=True), default=utcnow)

    practice = relationship("Practice", back_populates="calls")
    lead     = relationship("Lead", back_populates="call", uselist=False)


class Lead(Base):
    __tablename__ = "leads"

    id              = Column(Integer, primary_key=True)
    call_id         = Column(Integer, ForeignKey("calls.id"), nullable=False, unique=True)
    name            = Column(String, nullable=True)
    intent          = Column(Enum(Intent), nullable=True)
    callback_number = Column(String, nullable=True)
    notes           = Column(Text, nullable=True)
    created_at      = Column(DateTime(timezone=True), default=utcnow)

    call         = relationship("Call", back_populates="lead")
    appointments = relationship("Appointment", back_populates="lead")


class Appointment(Base):
    __tablename__ = "appointments"

    id             = Column(Integer, primary_key=True)
    lead_id        = Column(Integer, ForeignKey("leads.id"), nullable=False)
    requested_slot = Column(DateTime(timezone=True), nullable=False)
    status         = Column(Enum(AppointmentStatus), default=AppointmentStatus.pending)
    confirmation   = Column(String, nullable=True)
    created_at     = Column(DateTime(timezone=True), default=utcnow)

    lead = relationship("Lead", back_populates="appointments")
