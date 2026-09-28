"""index_calls_practice_id

Add the missing index on calls.practice_id — every /calls list and the bot's
own inbound lookup filter on it, and it had no index despite being an FK.

transcript_turns.call_id is not indexed separately here: the composite
index added alongside that table, ix_transcript_turns_call_id_created_at
(call_id, created_at), already serves plain call_id lookups via its
leftmost column, so a second single-column index would just be redundant.

Revision ID: d7e2a4c81f93
Revises: c4a8e19f6b2d
Create Date: 2026-09-18 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd7e2a4c81f93'
down_revision: Union[str, None] = 'c4a8e19f6b2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_calls_practice_id', 'calls', ['practice_id'])


def downgrade() -> None:
    op.drop_index('ix_calls_practice_id', table_name='calls')
