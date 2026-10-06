"""One-time authorization to save a card on file for one appointment.

The raw token is never stored. Only its hash is. The row is bound to one
spa, one appointment, and one external customer link.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.external_customer_link import ExternalCustomerLink
    from app.models.spa_account import SpaAccount


class CardEntryToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "card_entry_tokens"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("spa_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_customer_link_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("external_customer_links.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(45), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submission_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tenant: Mapped["SpaAccount"] = relationship()
    appointment: Mapped["Appointment"] = relationship()
    external_customer_link: Mapped["ExternalCustomerLink"] = relationship()
