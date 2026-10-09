"""Tenant-owned proposals, immutable approvals, test delivery and audit records."""
import uuid
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

DATA = JSON().with_variant(JSONB(), "postgresql")


class CaraPreference(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "cara_preferences"
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("spa_accounts.id"), unique=True)
    approved_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    value: Mapped[dict] = mapped_column(DATA, nullable=False, default=dict)


class CaraCampaign(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "cara_campaigns"
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("spa_accounts.id"), index=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    request: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    proposal: Mapped[dict] = mapped_column(DATA, nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_hash: Mapped[str | None] = mapped_column(String(64))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaraDelivery(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "cara_deliveries"
    __table_args__ = (UniqueConstraint("campaign_id", "contact_id", name="uq_cara_campaign_contact"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("spa_accounts.id"), index=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("cara_campaigns.id"), index=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("contacts.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str | None] = mapped_column(Text)
    booking: Mapped[dict | None] = mapped_column(DATA)
    booking_session: Mapped[dict | None] = mapped_column(DATA)


class CaraPackage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "cara_packages"
    __table_args__ = (UniqueConstraint("campaign_id", "contact_id", name="uq_cara_package_contact"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("spa_accounts.id"), index=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("cara_campaigns.id"))
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("contacts.id"))
    offer: Mapped[dict] = mapped_column(DATA, nullable=False)


class CaraAudit(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "cara_audit"
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("spa_accounts.id"), index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("cara_campaigns.id"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict] = mapped_column(DATA, nullable=False, default=dict)
