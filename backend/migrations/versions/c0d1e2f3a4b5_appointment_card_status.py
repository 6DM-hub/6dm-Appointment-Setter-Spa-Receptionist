"""Separate saved-card status from appointment status.

Revision ID: c0d1e2f3a4b5
Revises: f7a8b9c0d1e2
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c0d1e2f3a4b5"
down_revision: Union[str, Sequence[str], None] = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None

_CARD_STATUS = (
    "not_required",
    "not_supported",
    "unknown",
    "pending_card",
    "card_confirmed",
    "failed",
)


def upgrade() -> None:
    card_status = sa.Enum(*_CARD_STATUS, name="card_status")
    card_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "appointments",
        sa.Column(
            "card_status",
            sa.Enum(*_CARD_STATUS, name="card_status", create_type=False),
            nullable=False,
            server_default="unknown",
        ),
    )


def downgrade() -> None:
    op.drop_column("appointments", "card_status")
    sa.Enum(name="card_status").drop(op.get_bind(), checkfirst=True)
