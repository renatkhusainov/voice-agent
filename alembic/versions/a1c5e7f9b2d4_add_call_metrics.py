"""add_call_metrics

One row per caller turn with per-stage latency offsets, keyed by
call_id + turn (app/services/latency.py, docs/notes/latency-budget.md).

Revision ID: a1c5e7f9b2d4
Revises: f3b9c2d4e5a1
Create Date: 2026-09-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c5e7f9b2d4'
down_revision: Union[str, None] = 'f3b9c2d4e5a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'call_metrics',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('call_id', sa.Integer(), nullable=False),
        sa.Column('turn', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=64), nullable=False),
        sa.Column('speech_end_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('vad_ms', sa.Integer(), nullable=True),
        sa.Column('stt_final_ms', sa.Integer(), nullable=True),
        sa.Column('turn_end_ms', sa.Integer(), nullable=True),
        sa.Column('llm_start_ms', sa.Integer(), nullable=True),
        sa.Column('llm_first_token_ms', sa.Integer(), nullable=True),
        sa.Column('llm_last_token_ms', sa.Integer(), nullable=True),
        sa.Column('tts_first_byte_ms', sa.Integer(), nullable=True),
        sa.Column('first_audio_out_ms', sa.Integer(), nullable=True),
        sa.Column('llm_inferences', sa.Integer(), nullable=False),
        sa.Column('tools', sa.JSON(), nullable=True),
        sa.Column('interrupted', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['call_id'], ['calls.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('call_id', 'turn', name='uq_call_metrics_call_id_turn'),
    )
    op.create_index('ix_call_metrics_label', 'call_metrics', ['label'])


def downgrade() -> None:
    op.drop_index('ix_call_metrics_label', table_name='call_metrics')
    op.drop_table('call_metrics')
