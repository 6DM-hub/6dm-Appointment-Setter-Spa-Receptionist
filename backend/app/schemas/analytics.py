import uuid
from datetime import datetime

from pydantic import BaseModel


class CallVolume(BaseModel):
    total: int
    inbound: int
    outbound: int
    completed: int
    failed: int


class BookingVolume(BaseModel):
    total: int
    scheduled: int
    confirmed: int
    cancelled: int
    completed: int
    no_show: int


class SentimentBreakdown(BaseModel):
    positive: int
    neutral: int
    negative: int
    unscored: int


class AnalyticsSummary(BaseModel):
    """Always describes exactly one scope: a single spa, or the 6DM sales
    workspace. `tenant_id` is echoed back so the dashboard can prove which one
    it is looking at."""

    tenant_id: uuid.UUID | None
    scope: str  # "spa" | "sales_workspace"
    window_start: datetime
    window_end: datetime
    calls: CallVolume
    bookings: BookingVolume
    sentiment: SentimentBreakdown
    average_call_duration_seconds: float | None
    booking_conversion_rate: float
    contacts_total: int
