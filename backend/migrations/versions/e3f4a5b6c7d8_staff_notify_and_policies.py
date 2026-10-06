"""Staff notification settings, booking policies, and callback requests.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, Sequence[str], None] = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "spa_accounts",
        sa.Column(
            "notification_settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "spa_accounts",
        sa.Column(
            "booking_policies",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_table(
        "follow_up_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("spa_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("spa_accounts.id", ondelete="SET NULL")),
        sa.Column("call_sid", sa.String(length=64), nullable=True),
        sa.Column("caller_phone", sa.String(length=32), nullable=True),
        sa.Column("caller_name", sa.String(length=255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("preferred_window", sa.String(length=255), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="callback"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("follow_up_requests")
    op.drop_column("spa_accounts", "booking_policies")
    op.drop_column("spa_accounts", "notification_settings")
