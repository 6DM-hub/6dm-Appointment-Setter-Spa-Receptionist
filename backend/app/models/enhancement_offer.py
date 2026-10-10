import uuid
from sqlalchemy import ForeignKey, String, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

class EnhancementOffer(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "enhancement_offers"
    __table_args__ = (UniqueConstraint("tenant_id", "intent_key", name="uq_enhancement_intent"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("spa_accounts.id", ondelete="CASCADE"), index=True)
    intent_key: Mapped[str] = mapped_column(String(128))
    customer_key: Mapped[str | None] = mapped_column(String(64), index=True)
    base_service: Mapped[str] = mapped_column(String(255))
    target_service: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="eligible")
    incremental_minor: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(3))
    facts: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    external_booking_id: Mapped[str | None] = mapped_column(String(255))
