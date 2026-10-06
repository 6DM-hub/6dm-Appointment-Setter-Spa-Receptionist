"""Make one local appointment authoritative for each call."""
from typing import Sequence, Union

from alembic import op


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_appointments_source_call",
        "appointments",
        ["source_call_id"],
        unique=True,
        postgresql_where="source_call_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index("uq_appointments_source_call", table_name="appointments")