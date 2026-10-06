from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, Boolean,
    DateTime, ForeignKey, Enum, JSON, Index, UniqueConstraint
)
from sqlalchemy.orm import DeclarativeBase, relationship
from pgvector.sqlalchemy import Vector
import enum

from app.rag.embed import DIM as EMBEDDING_DIM


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


class TranscriptRole(str, enum.Enum):
    user      = "user"
    assistant = "assistant"
    tool      = "tool"  # a tool call and its result, on a live call (app/agent/live.py)


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
    practice_id    = Column(Integer, ForeignKey("practices.id"), nullable=False, index=True)
    caller_number  = Column(String, nullable=False)
    started_at     = Column(DateTime(timezone=True), nullable=False)
    ended_at       = Column(DateTime(timezone=True), nullable=True)
    status         = Column(Enum(CallStatus), nullable=False)
    recording_url  = Column(String(500), nullable=True)
    created_at     = Column(DateTime(timezone=True), default=utcnow)

    practice = relationship("Practice", back_populates="calls")
    lead     = relationship("Lead", back_populates="call", uselist=False)
    transcript_turns = relationship(
        "TranscriptTurn", back_populates="call", order_by="TranscriptTurn.created_at"
    )


class TranscriptTurn(Base):
    __tablename__ = "transcript_turns"
    __table_args__ = (
        # Every eval-suite query starts with "turns for this call, in order"
        Index("ix_transcript_turns_call_id_created_at", "call_id", "created_at"),
    )

    id         = Column(Integer, primary_key=True)
    call_id    = Column(Integer, ForeignKey("calls.id"), nullable=False)
    role       = Column(Enum(TranscriptRole), nullable=False)
    text       = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    call = relationship("Call", back_populates="transcript_turns")


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
    # The appointment in the scheduling system of record (FHIR,
    # docs/fhir-mapping.md). This row is the shadow: which call booked it.
    fhir_appointment_id = Column(String(64), nullable=True, index=True)
    created_at     = Column(DateTime(timezone=True), default=utcnow)

    lead = relationship("Lead", back_populates="appointments")


class CallMetric(Base):
    """Latency of one caller turn on a live call (app/services/latency.py).

    Every *_ms column is an offset from the same anchor: the moment the
    caller actually stopped speaking, estimated as the VAD's decision time
    minus its stop_secs window. So each column answers "how long after the
    caller stopped talking did X happen", and a stage's own duration is the
    difference between two columns. NULL means the stage never happened in
    that turn (no tool call, or the caller interrupted before any audio).
    """

    __tablename__ = "call_metrics"
    __table_args__ = (UniqueConstraint("call_id", "turn", name="uq_call_metrics_call_id_turn"),)

    id                 = Column(Integer, primary_key=True)
    call_id            = Column(Integer, ForeignKey("calls.id"), nullable=False)
    turn               = Column(Integer, nullable=False)
    # Which configuration produced this row (LATENCY_LABEL), so "before" and
    # "after" runs of an optimization can be compared from one table.
    label              = Column(String(64), nullable=False, index=True)
    speech_end_at      = Column(DateTime(timezone=True), nullable=True)
    vad_ms             = Column(Integer, nullable=True)  # VAD decides speech ended
    stt_final_ms       = Column(Integer, nullable=True)  # last final transcript
    turn_end_ms        = Column(Integer, nullable=True)  # turn released to the LLM
    llm_start_ms       = Column(Integer, nullable=True)  # first inference starts
    llm_first_token_ms = Column(Integer, nullable=True)  # first text token, any inference
    llm_last_token_ms  = Column(Integer, nullable=True)  # last inference of the turn ends
    tts_first_byte_ms  = Column(Integer, nullable=True)  # first TTS audio chunk
    first_audio_out_ms = Column(Integer, nullable=True)  # bot starts speaking (output transport)
    llm_inferences     = Column(Integer, nullable=False, default=0)
    tools              = Column(JSON, nullable=True)  # [{name, start_ms, end_ms}]
    interrupted        = Column(Boolean, nullable=False, default=False)
    created_at         = Column(DateTime(timezone=True), default=utcnow)


# ── Knowledge base (app/rag/, docs/notes/rag.md) ─────────────────────────────
class Document(Base):
    """One knowledge-base markdown file for one practice. `source` is the
    file's path relative to the KB folder. `content_hash` covers the text,
    the chunker version and the embedding model, so ingest skips a doc only
    when none of the three changed (scripts/ingest.py)."""

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("practice_id", "source", name="uq_documents_practice_id_source"),)

    id           = Column(Integer, primary_key=True)
    practice_id  = Column(Integer, ForeignKey("practices.id"), nullable=False, index=True)
    title        = Column(String(300), nullable=False)
    source       = Column(String(300), nullable=False)
    content_hash = Column(String(64), nullable=False)
    created_at   = Column(DateTime(timezone=True), default=utcnow)
    updated_at   = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    chunks = relationship("Chunk", back_populates="document", cascade="all, delete-orphan",
                          order_by="Chunk.ordinal")


class Chunk(Base):
    """A searchable piece of a Document. Postgres-only parts live in the
    migration, not here: `tsv` is a generated column
    (to_tsvector('english', text)) that the ORM never writes, plus the HNSW
    index on embedding and the GIN index on tsv. SQLite (the unit-test DB)
    can create this table without them."""

    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_id_ordinal"),)

    id           = Column(Integer, primary_key=True)
    document_id  = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    ordinal      = Column(Integer, nullable=False)
    text         = Column(Text, nullable=False)
    token_count  = Column(Integer, nullable=False)
    # sha256 of (embedding model, text): an unchanged chunk keeps its stored
    # embedding on re-ingest instead of a new API call.
    content_hash = Column(String(64), nullable=False, index=True)
    embedding    = Column(Vector(EMBEDDING_DIM), nullable=False)

    document = relationship("Document", back_populates="chunks")
