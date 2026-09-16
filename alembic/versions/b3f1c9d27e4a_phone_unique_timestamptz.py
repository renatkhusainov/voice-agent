"""phone_unique_timestamptz

Sync the schema with the models:
- add the unique constraint on practices.phone (model had unique=True, init migration missed it)
- drop calls.transcript_url (removed from the model; replaced by recording_url)
- store all timestamps as timestamptz (existing naive values are treated as UTC)

Revision ID: b3f1c9d27e4a
Revises: a722dd32a956
Create Date: 2026-09-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f1c9d27e4a'
down_revision: Union[str, None] = 'a722dd32a956'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TIMESTAMP_COLUMNS = [
    ("practices", "created_at"),
    ("calls", "started_at"),
    ("calls", "ended_at"),
    ("calls", "created_at"),
    ("leads", "created_at"),
    ("appointments", "requested_slot"),
    ("appointments", "created_at"),
]


def upgrade() -> None:
    op.create_unique_constraint('practices_phone_key', 'practices', ['phone'])
    op.drop_column('calls', 'transcript_url')

    for table, column in TIMESTAMP_COLUMNS:
        op.alter_column(
            table, column,
            type_=sa.DateTime(timezone=True),
            existing_type=sa.DateTime(),
            postgresql_using=f"{column} AT TIME ZONE 'UTC'",
        )


def downgrade() -> None:
    for table, column in TIMESTAMP_COLUMNS:
        op.alter_column(
            table, column,
            type_=sa.DateTime(),
            existing_type=sa.DateTime(timezone=True),
            postgresql_using=f"{column} AT TIME ZONE 'UTC'",
        )

    op.add_column('calls', sa.Column('transcript_url', sa.String(), nullable=True))
    op.drop_constraint('practices_phone_key', 'practices', type_='unique')
