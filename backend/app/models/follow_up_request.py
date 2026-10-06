"""A stored customer callback. Not a staff notification and never a card record."""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.spa_account import SpaAccount


class FollowUpRequest(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "follow_up_requests"

    spa_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("spa_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    call_sid: Mapped[str | None] = mapped_column(String(64))
    caller_phone: Mapped[str | None] = mapped_column(String(32))
    caller_name: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(Text)
    preferred_window: Mapped[str | None] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="callback")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")

    spa: Mapped["SpaAccount | None"] = relationship()
