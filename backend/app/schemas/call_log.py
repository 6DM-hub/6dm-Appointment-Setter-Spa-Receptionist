import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.call_log import CallDirection, CallStatus
from app.schemas.common import ORMModel


class ContactSummary(ORMModel):
    """Minimal contact projection embedded in call logs for display purposes."""

    id: uuid.UUID
    full_name: str
    phone_number: str


class CallLogRead(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    # The spa this call was answered for; NULL for 6DM outbound sales calls.
    tenant_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    # Requires the caller to eager-load CallLog.contact (selectinload); the
    # relationship is lazy by default and async lazy loads raise MissingGreenlet.
    contact: ContactSummary | None = None
    twilio_call_sid: str
    direction: CallDirection
    status: CallStatus
    from_number: str
    to_number: str
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    transcript: str | None
    recording_url: str | None
    ai_summary: str | None
    primary_language: str | None
    ai_analysis: dict[str, Any]
    created_at: datetime


class CallLogUpdate(BaseModel):
    status: CallStatus | None = None
    transcript: str | None = None
    recording_url: str | None = None
    ai_summary: str | None = None
    ai_analysis: dict[str, Any] | None = None


class OutboundCallRequest(BaseModel):
    to_number: str = Field(..., description="Destination in E.164 format")
    from_number: str | None = Field(
        None, description="Optional configured caller ID in E.164 format"
    )
    contact_id: uuid.UUID | None = None
    # Optional context injected into the agent's system prompt for this call
    call_objective: str | None = Field(
        None, max_length=2000, examples=["Confirm tomorrow's 2pm appointment"]
    )


class OutboundCallResponse(BaseModel):
    call_sid: str
    call_log_id: uuid.UUID
    status: str