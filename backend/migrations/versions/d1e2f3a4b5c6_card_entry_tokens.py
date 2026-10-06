"""One-time hashed card-entry tokens.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "card_entry_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("spa_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("appointment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_customer_link_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("external_customer_links.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(45), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submission_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_card_entry_tokens_tenant_id", "card_entry_tokens", ["tenant_id"])
    op.create_index("ix_card_entry_tokens_appointment_id", "card_entry_tokens", ["appointment_id"])
    op.create_index("ix_card_entry_tokens_external_customer_link_id", "card_entry_tokens", ["external_customer_link_id"])
    op.create_index("uq_card_entry_tokens_token_hash", "card_entry_tokens", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_card_entry_tokens_token_hash", table_name="card_entry_tokens")
    op.drop_index("ix_card_entry_tokens_external_customer_link_id", table_name="card_entry_tokens")
    op.drop_index("ix_card_entry_tokens_appointment_id", table_name="card_entry_tokens")
    op.drop_index("ix_card_entry_tokens_tenant_id", table_name="card_entry_tokens")
    op.drop_table("card_entry_tokens")
