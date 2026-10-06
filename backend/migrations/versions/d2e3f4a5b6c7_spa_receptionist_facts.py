"""Dashboard fields the receptionist may quote as facts.

Adds spa-owned copy and policy configuration so the voice agent never has to
invent address, amenities, packages, upsells, or payment instructions.

Revision ID: d2e3f4a5b6c7
Revises: a1b2c3d4e5f6
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("spa_accounts", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "spa_accounts",
        sa.Column("public_phone", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "spa_accounts",
        sa.Column("cancellation_policy", sa.Text(), nullable=True),
    )
    op.add_column(
        "spa_accounts",
        sa.Column(
            "amenities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "spa_accounts",
        sa.Column(
            "packages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "spa_accounts",
        sa.Column(
            "upsell_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "spa_accounts",
        sa.Column(
            "payment_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text(
                '\'{"card_required": false, "collection_mode": "none"}\'::jsonb'
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("spa_accounts", "payment_policy")
    op.drop_column("spa_accounts", "upsell_rules")
    op.drop_column("spa_accounts", "packages")
    op.drop_column("spa_accounts", "amenities")
    op.drop_column("spa_accounts", "cancellation_policy")
    op.drop_column("spa_accounts", "public_phone")
    op.drop_column("spa_accounts", "description")
