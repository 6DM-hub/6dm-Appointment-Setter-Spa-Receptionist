import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.contact import ContactBase
from app.schemas.common import ORMModel


class LeadCreate(ContactBase):
    """A B2B prospect for the 6DM outbound sales agent. Same shape as a
    contact — `extra_metadata` carries company, source, tags."""


class LeadUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    phone_number: str | None = None
    email: str | None = None
    extra_metadata: dict | None = None


class LeadRead(ORMModel, ContactBase):
    id: uuid.UUID
    owner_id: uuid.UUID | None
    full_name: str
    created_at: datetime
    updated_at: datetime
    # Pipeline context, aggregated per lead by the list/detail endpoints.
    call_count: int = 0
    last_call_at: datetime | None = None
    upcoming_appointment_at: datetime | None = Field(
        None, description="Start of the next scheduled/confirmed presentation"
    )
