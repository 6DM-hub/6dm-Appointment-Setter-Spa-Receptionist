"""One appointment per booking intent, replacing one per call.

The previous revision made `source_call_id` unique, which stopped a call
producing two appointments — but a caller is allowed to book two separate
appointments in one call, and a repeated confirmation raised IntegrityError
rather than resolving to the row already written.

`booking_intent_key` is "<call_sid>:<booking_id>": stable across any number of
draft edits within one intent, distinct for an explicitly requested second
appointment.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column("booking_intent_key", sa.String(length=128), nullable=True),
    )
    # Backfill so appointments already written by a live call keep taking part
    # in idempotency instead of being invisible to it. One row per call wins,
    # which is exactly what the old unique index guaranteed anyway.
    # The suffix is bound rather than inlined: op.execute() puts a raw string
    # through sa.text(), which reads a literal ":legacy" as a bind parameter
    # named "legacy" and then fails with "A value is required for bind
    # parameter 'legacy'".
    op.execute(
        sa.text(
            """
            UPDATE appointments AS a
            SET booking_intent_key = c.twilio_call_sid || :suffix
            FROM call_logs AS c
            WHERE a.source_call_id = c.id
              AND a.booking_intent_key IS NULL
            """
        ).bindparams(suffix=":legacy")
    )
    op.create_index(
        "uq_appointments_booking_intent",
        "appointments",
        ["booking_intent_key"],
        unique=True,
        postgresql_where=sa.text("booking_intent_key IS NOT NULL"),
    )
    # Superseded: it forbids the legitimate "two separate appointments in one
    # call" case that the intent key expresses correctly.
    op.drop_index("uq_appointments_source_call", table_name="appointments")


def downgrade() -> None:
    op.create_index(
        "uq_appointments_source_call",
        "appointments",
        ["source_call_id"],
        unique=True,
        postgresql_where=sa.text("source_call_id IS NOT NULL"),
    )
    op.drop_index("uq_appointments_booking_intent", table_name="appointments")
    op.drop_column("appointments", "booking_intent_key")
