"""Local contact mapped to one provider customer, inside one spa.

The provider profile is authoritative. This row only remembers which external
customer we already resolved for this tenant. The same external id may exist
at another spa; tenant_id is part of both uniqueness rules.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.contact import Contact
    from app.models.spa_account import SpaAccount


class ExternalCustomerLink(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "external_customer_links"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "external_customer_id",
            name="uq_external_customer_tenant_provider_external",
        ),
        UniqueConstraint(
            "tenant_id",
            "contact_id",
            "provider",
            name="uq_external_customer_tenant_contact_provider",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("spa_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_customer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    last_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    tenant: Mapped["SpaAccount"] = relationship()
    contact: Mapped["Contact"] = relationship()
