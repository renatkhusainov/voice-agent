"""add_transcript_turns

Add transcript_turns: one row per user/assistant turn, so the eval suite can
query a call's transcript in order (or across calls) with plain indexed SQL
instead of parsing a JSON blob.

Revision ID: c4a8e19f6b2d
Revises: b3f1c9d27e4a
Create Date: 2026-09-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4a8e19f6b2d'
down_revision: Union[str, None] = 'b3f1c9d27e4a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'transcript_turns',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('call_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.Enum('user', 'assistant', name='transcriptrole'), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['call_id'], ['calls.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_transcript_turns_call_id_created_at',
        'transcript_turns',
        ['call_id', 'created_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_transcript_turns_call_id_created_at', table_name='transcript_turns')
    op.drop_table('transcript_turns')
    sa.Enum(name='transcriptrole').drop(op.get_bind(), checkfirst=True)
