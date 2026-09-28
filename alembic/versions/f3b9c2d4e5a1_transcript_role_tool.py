"""transcript_role_tool

Add 'tool' to the transcriptrole enum: a live call's tool calls
(app/agent/live.py) are written to transcript_turns alongside what the
caller and the agent said, so a transcript shows *why* the agent said what
it did — "book_appointment(...) -> pending_confirmation" right before the
read-back, not just the read-back.

Revision ID: f3b9c2d4e5a1
Revises: d7e2a4c81f93
Create Date: 2026-09-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f3b9c2d4e5a1'
down_revision: Union[str, None] = 'd7e2a4c81f93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL 12+ allows ADD VALUE inside a transaction (the new value
    # just can't be *used* in that same transaction, and nothing here does).
    op.execute("ALTER TYPE transcriptrole ADD VALUE IF NOT EXISTS 'tool'")


def downgrade() -> None:
    # PostgreSQL can't drop a single enum value, so: delete the rows that use
    # it (they're derived data — the tool calls also live in the logs and in
    # leads/appointments), then rebuild the type without it.
    op.execute("DELETE FROM transcript_turns WHERE role = 'tool'")
    op.execute("ALTER TYPE transcriptrole RENAME TO transcriptrole_old")
    op.execute("CREATE TYPE transcriptrole AS ENUM ('user', 'assistant')")
    op.execute(
        "ALTER TABLE transcript_turns ALTER COLUMN role TYPE transcriptrole "
        "USING role::text::transcriptrole"
    )
    op.execute("DROP TYPE transcriptrole_old")
