"""Google Calendar booking adapter."""
import logging

from app.services.booking_adapters.base import (
    AvailabilityVerdict,
    BookingContext,
    BookingProviderError,
    ExternalBooking,
    LocalCalendarAdapter,
)
from app.services.google_calendar import (
    GoogleCalendarError,
    create_event,
    delete_event,
    freebusy,
    update_event,
)

logger = logging.getLogger(__name__)


class GoogleCalendarAdapter(LocalCalendarAdapter):
    provider = "google_calendar"
    calendar_label = "the Google Calendar"

    write_back_supported = True
    # Google Calendar stores events, not a customer directory or saved cards.
    supports_customer_lookup = False
    supports_customer_creation = False
    supports_saved_payment_method_lookup = False

    def __init__(
        self,
        calendar_id: str | None = None,
        calendar_label: str | None = None,
        default_title: str | None = None,
        connection=None,
        timezone_name: str = "America/Chicago",
    ) -> None:
        super().__init__(calendar_label=calendar_label, default_title=default_title)
        self.calendar_id = calendar_id
        self.connection = connection
        self.timezone_name = timezone_name

    @property
    def is_configured(self) -> bool:
        return bool(self.calendar_id and self.connection and self.write_back_supported)

    async def create_booking(self, ctx: BookingContext) -> ExternalBooking:
        if not self.is_configured:
            logger.info(
                "Google Calendar write-back unavailable (calendar_id=%r, "
                "supported=%s); booking %s held locally only.",
                self.calendar_id,
                self.write_back_supported,
                ctx.start.isoformat(),
            )
            return ExternalBooking(provider=self.provider, external_id=None)

        title = f"{ctx.service_description or ctx.title} - {ctx.customer_name or ctx.customer_phone}"
        description = "\n".join(
            value for value in (
                f"Customer: {ctx.customer_name or 'Unknown'}",
                f"Phone: {ctx.customer_phone}",
                f"Email: {ctx.customer_email}" if ctx.customer_email else None,
                f"Service: {ctx.service_description or ctx.title}",
                f"Notes: {ctx.notes}" if ctx.notes else None,
            ) if value
        )
        try:
            event_id = await create_event(
                self.connection,
                title=title,
                start=ctx.start,
                end=ctx.end,
                timezone_name=self.timezone_name,
                description=description,
                event_id=ctx.booking_reference,
            )
        except GoogleCalendarError as exc:
            logger.exception("Google Calendar create failed for %s", self.calendar_id)
            raise BookingProviderError(str(exc)) from exc
        return ExternalBooking(provider=self.provider, external_id=event_id)

    async def check_availability(self, ctx: BookingContext):
        if not self.is_configured:
            return await super().check_availability(ctx)
        try:
            busy = await freebusy(
                self.connection,
                calendar_id=self.calendar_id,
                start=ctx.start,
                end=ctx.end,
                timezone_name=self.timezone_name,
            )
        except GoogleCalendarError as exc:
            logger.exception("Google Calendar availability failed for %s", self.calendar_id)
            raise BookingProviderError(str(exc)) from exc
        if busy:
            return AvailabilityVerdict.no("That time is already occupied on the calendar.")
        return AvailabilityVerdict.ok()

    async def move_booking(self, booking, start, end):
        if not self.is_configured or not booking.external_id:
            return booking
        try:
            await update_event(
                self.connection,
                booking.external_id,
                start=start,
                end=end,
                timezone_name=self.timezone_name,
            )
        except GoogleCalendarError:
            logger.exception("Google Calendar update failed for %s", booking.external_id)
        return booking

    async def cancel_booking(self, booking) -> None:
        if not self.is_configured or not booking.external_id:
            return
        try:
            await delete_event(self.connection, booking.external_id)
        except GoogleCalendarError:
            logger.exception("Google Calendar delete failed for %s", booking.external_id)
