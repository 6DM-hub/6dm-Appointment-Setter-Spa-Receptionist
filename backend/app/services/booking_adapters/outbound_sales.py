"""6DM Sales Agent booking strategy.

Used strictly by the outbound B2B agent. There is exactly one destination —
Dominic's Google Calendar — and no tenant configuration: spa accounts can never
select this adapter, and it can never be reached from an inbound spa call.
"""
from app.core.config import settings
from app.services.booking_adapters.google_calendar import GoogleCalendarAdapter


class OutboundSalesAdapter(GoogleCalendarAdapter):
    provider = "google_calendar"
    calendar_label = "Dominic's Google Calendar"
    default_title = "6DM Sales Presentation"

    def __init__(self) -> None:
        super().__init__(
            calendar_id=settings.SALES_GOOGLE_CALENDAR_ID
            or settings.GOOGLE_CALENDAR_ID,
            calendar_label=self.calendar_label,
            default_title=self.default_title,
        )
        self.default_duration_minutes = settings.SALES_PRESENTATION_DURATION_MINUTES
