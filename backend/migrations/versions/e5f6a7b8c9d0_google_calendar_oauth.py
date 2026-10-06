"""Add per-spa Google Calendar OAuth connections and Central Time default."""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "google_calendar_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("spa_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("google_account_email", sa.String(length=320), nullable=True),
        sa.Column("selected_calendar_id", sa.String(length=512), nullable=True),
        sa.Column("credentials", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("scopes", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("status", sa.String(length=64), server_default="connected", nullable=False),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["spa_id"], ["spa_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spa_id", name="uq_google_calendar_connection_spa"),
    )
    op.create_index(
        "ix_google_calendar_connections_spa_id",
        "google_calendar_connections",
        ["spa_id"],
    )
    op.execute(
        "UPDATE spa_accounts SET timezone = 'America/Chicago' WHERE timezone = 'UTC'"
    )
    op.alter_column(
        "spa_accounts",
        "timezone",
        server_default="America/Chicago",
    )


def downgrade() -> None:
    op.alter_column("spa_accounts", "timezone", server_default="UTC")
    op.drop_index("ix_google_calendar_connections_spa_id", table_name="google_calendar_connections")
    op.drop_table("google_calendar_connections")