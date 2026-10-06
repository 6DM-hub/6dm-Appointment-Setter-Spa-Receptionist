import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.spa_account import SpaAccount


class ServiceCategory(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "service_categories"
    __table_args__ = (UniqueConstraint("spa_id", "name", name="uq_service_category_spa_name"),)

    spa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spa_accounts.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    spa: Mapped["SpaAccount"] = relationship()
    services: Mapped[list["Service"]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class Service(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "services"

    spa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spa_accounts.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_categories.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[str | None] = mapped_column(String(32))
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    spa: Mapped["SpaAccount"] = relationship()
    category: Mapped["ServiceCategory"] = relationship(back_populates="services")