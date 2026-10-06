import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.models.appointment import AppointmentStatus, CardStatus
from app.schemas.common import ORMModel


class AppointmentBase(BaseModel):
    title: str = Field(..., max_length=255)
    description: str | None = None
    start_time: datetime
    end_time: datetime
    status: AppointmentStatus = AppointmentStatus.SCHEDULED

    @model_validator(mode="after")
    def validate_times(self) -> "AppointmentBase":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class AppointmentCreate(AppointmentBase):
    contact_id: uuid.UUID
    source_call_id: uuid.UUID | None = None


class AppointmentUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    status: AppointmentStatus | None = None


class AppointmentRead(ORMModel, AppointmentBase):
    id: uuid.UUID
    # Exactly one of these is set: `user_id` for Dominic's sales calendar,
    # `tenant_id` for a spa's service calendar.
    user_id: uuid.UUID | None
    tenant_id: uuid.UUID | None
    contact_id: uuid.UUID
    source_call_id: uuid.UUID | None
    # Where the booking was mirrored (google_calendar, mindbody, ...) and its id
    # in that system, when the provider supports write-back.
    booking_provider: str | None = None
    external_booking_id: str | None = None
    card_status: CardStatus = CardStatus.UNKNOWN
    created_at: datetime
    updated_at: datetime