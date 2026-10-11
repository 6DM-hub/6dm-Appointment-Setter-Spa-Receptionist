"""Persist Square catalog identity on normalized services.

Revision ID: a105_square_catalog
Revises: a104_booking_escalations
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a105_square_catalog"
down_revision: Union[str, Sequence[str], None] = "a104_booking_escalations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("services", sa.Column("square_item_id", sa.String(length=255), nullable=True))
    op.add_column("services", sa.Column("square_variation_id", sa.String(length=255), nullable=True))
    op.add_column("services", sa.Column("square_variation_version", sa.BigInteger(), nullable=True))
    op.add_column(
        "services",
        sa.Column(
            "voice_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_index("ix_services_square_item_id", "services", ["square_item_id"])
    op.create_index("ix_services_square_variation_id", "services", ["square_variation_id"])
    op.create_unique_constraint(
        "uq_services_spa_square_variation",
        "services",
        ["spa_id", "square_variation_id"],
    )

    op.execute(
        sa.text(
            """
            WITH metadata_candidates AS (
                SELECT
                    service.id AS service_id,
                    item - ARRAY[
                        'id', 'name', 'duration_minutes', 'price', 'description',
                        'category', 'category_id', 'is_active', 'square_item_id',
                        'square_variation_id', 'square_variation_version'
                    ]::text[] AS voice_metadata,
                    count(*) OVER (PARTITION BY service.id) AS match_count,
                    row_number() OVER (PARTITION BY service.id ORDER BY service.id) AS match_rank
                FROM spa_accounts spa
                CROSS JOIN LATERAL jsonb_array_elements(
                    CASE WHEN jsonb_typeof(spa.services) = 'array'
                         THEN spa.services ELSE '[]'::jsonb END
                ) item
                JOIN services service ON service.spa_id = spa.id
                 AND lower(trim(service.name)) = lower(trim(item->>'name'))
                 AND service.duration_minutes = CASE
                     WHEN (item->>'duration_minutes') ~ '^[0-9]+$'
                     THEN GREATEST((item->>'duration_minutes')::integer, 5)
                     ELSE 60
                 END
            )
            UPDATE services service
               SET voice_metadata = candidate.voice_metadata
              FROM metadata_candidates candidate
             WHERE service.id = candidate.service_id
               AND candidate.match_count = 1
               AND candidate.match_rank = 1
            """
        )
    )

    # Preserve mappings already saved in the tenant JSON menu. The duration
    # qualifier keeps equal names with different lengths distinct.
    op.execute(
        sa.text(
            """
            WITH raw_candidates AS (
                SELECT DISTINCT
                    spa.id AS spa_id,
                    service.id AS service_id,
                    NULLIF(item->>'square_item_id', '') AS square_item_id,
                    NULLIF(item->>'square_variation_id', '') AS square_variation_id,
                    CASE
                        WHEN (item->>'square_variation_version') ~ '^[0-9]+$'
                        THEN (item->>'square_variation_version')::bigint
                        ELSE NULL
                    END AS square_variation_version
                FROM spa_accounts spa
                CROSS JOIN LATERAL jsonb_array_elements(
                    CASE WHEN jsonb_typeof(spa.services) = 'array'
                         THEN spa.services ELSE '[]'::jsonb END
                ) item
                JOIN services service ON service.spa_id = spa.id
                 AND lower(trim(service.name)) = lower(trim(item->>'name'))
                 AND service.duration_minutes = CASE
                     WHEN (item->>'duration_minutes') ~ '^[0-9]+$'
                     THEN GREATEST((item->>'duration_minutes')::integer, 5)
                     ELSE 60
                 END
                WHERE NULLIF(item->>'square_variation_id', '') IS NOT NULL
            ), candidates AS (
                SELECT raw_candidates.*,
                    row_number() OVER (
                        PARTITION BY spa_id, square_variation_id
                        ORDER BY service_id
                    ) AS mapping_rank,
                    count(*) OVER (PARTITION BY service_id) AS service_match_count
                FROM raw_candidates
            )
            UPDATE services service
               SET square_item_id = candidate.square_item_id,
                   square_variation_id = candidate.square_variation_id,
                   square_variation_version = candidate.square_variation_version
              FROM candidates candidate
             WHERE service.id = candidate.service_id
               AND candidate.mapping_rank = 1
               AND candidate.service_match_count = 1
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint("uq_services_spa_square_variation", "services", type_="unique")
    op.drop_index("ix_services_square_variation_id", table_name="services")
    op.drop_index("ix_services_square_item_id", table_name="services")
    op.drop_column("services", "square_variation_version")
    op.drop_column("services", "square_variation_id")
    op.drop_column("services", "square_item_id")
    op.drop_column("services", "voice_metadata")
