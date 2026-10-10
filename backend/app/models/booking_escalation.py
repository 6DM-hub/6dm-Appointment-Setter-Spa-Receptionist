import uuid
from sqlalchemy import ForeignKey, String, JSON, UniqueConstraint, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, UUIDPrimaryKeyMixin, TimestampMixin


class BookingEscalation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "booking_escalations"
    __table_args__ = (UniqueConstraint("tenant_id", "call_reference", name="uq_escalation_tenant_call"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("spa_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    call_reference: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    priority: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False)
    delivery: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    history: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    delivery_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
