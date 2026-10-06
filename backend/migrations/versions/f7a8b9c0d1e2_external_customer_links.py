"""Tenant-scoped local-to-external customer links.

Revision ID: f7a8b9c0d1e2
Revises: e3f4a5b6c7d8
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_customer_links",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("spa_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_customer_id", sa.String(length=255), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "external_customer_id",
            name="uq_external_customer_tenant_provider_external",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "contact_id",
            "provider",
            name="uq_external_customer_tenant_contact_provider",
        ),
    )
    op.create_index(
        "ix_external_customer_links_tenant_id",
        "external_customer_links",
        ["tenant_id"],
    )
    op.create_index(
        "ix_external_customer_links_contact_id",
        "external_customer_links",
        ["contact_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_external_customer_links_contact_id", table_name="external_customer_links")
    op.drop_index("ix_external_customer_links_tenant_id", table_name="external_customer_links")
    op.drop_table("external_customer_links")
