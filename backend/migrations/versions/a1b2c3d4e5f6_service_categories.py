"""Add tenant service categories and normalized services."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


DEFAULT_CATEGORIES = (
    "Facial",
    "Massage",
    "MedSpa",
    "Wellness Treatment",
    "Waxing",
    "Spa Packages",
)


def upgrade() -> None:
    op.create_table(
        "service_categories",
        sa.Column("spa_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["spa_id"], ["spa_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spa_id", "name", name="uq_service_category_spa_name"),
    )
    op.create_index("ix_service_categories_spa_id", "service_categories", ["spa_id"])

    op.create_table(
        "services",
        sa.Column("spa_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price", sa.String(length=32), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), server_default="60", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["spa_id"], ["spa_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["service_categories.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_services_spa_id", "services", ["spa_id"])
    op.create_index("ix_services_category_id", "services", ["category_id"])

    for name in DEFAULT_CATEGORIES:
        op.execute(
            sa.text(
                "INSERT INTO service_categories (spa_id, name) "
                "SELECT id, :name FROM spa_accounts "
                "ON CONFLICT (spa_id, name) DO NOTHING"
            ).bindparams(name=name)
        )

    op.execute(
        sa.text(
            """
            INSERT INTO service_categories (spa_id, name)
            SELECT DISTINCT spa.id, 'Uncategorized'
            FROM spa_accounts spa
            WHERE jsonb_typeof(spa.services) = 'array'
              AND EXISTS (
                SELECT 1 FROM jsonb_array_elements(spa.services) item
                WHERE COALESCE(NULLIF(item->>'category', ''), '') = ''
              )
            ON CONFLICT (spa_id, name) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO services (
                spa_id, category_id, name, description, price, duration_minutes, is_active
            )
            SELECT
                spa.id,
                category.id,
                item->>'name',
                NULLIF(item->>'description', ''),
                NULLIF(item->>'price', ''),
                GREATEST(COALESCE(NULLIF(item->>'duration_minutes', '')::integer, 60), 5),
                true
            FROM spa_accounts spa
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(spa.services) = 'array' THEN spa.services ELSE '[]'::jsonb END
            ) item
            JOIN service_categories category
              ON category.spa_id = spa.id
             AND category.name = COALESCE(NULLIF(item->>'category', ''), 'Uncategorized')
            WHERE NULLIF(item->>'name', '') IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_services_category_id", table_name="services")
    op.drop_index("ix_services_spa_id", table_name="services")
    op.drop_table("services")
    op.drop_index("ix_service_categories_spa_id", table_name="service_categories")
    op.drop_table("service_categories")