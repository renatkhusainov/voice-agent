"""appointment_fhir_id

appointments.fhir_appointment_id: scheduling moved to FHIR (the system of
record, docs/fhir-mapping.md); the Postgres row is now a shadow that records
which call booked which FHIR Appointment.

Revision ID: b7d2e4f6a8c1
Revises: a1c5e7f9b2d4
Create Date: 2026-10-01 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d2e4f6a8c1'
down_revision: Union[str, None] = 'a1c5e7f9b2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('appointments', sa.Column('fhir_appointment_id', sa.String(length=64), nullable=True))
    op.create_index('ix_appointments_fhir_appointment_id', 'appointments', ['fhir_appointment_id'])


def downgrade() -> None:
    op.drop_index('ix_appointments_fhir_appointment_id', table_name='appointments')
    op.drop_column('appointments', 'fhir_appointment_id')
