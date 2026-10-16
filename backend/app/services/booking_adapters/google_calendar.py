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

# Google failures that happen before, or instead of, the change being applied.
# Anything else (a generic API/server error) may have been applied: unknown.
_DEFINITE_GOOGLE_REFUSALS = {
    "authorization_required", "token_expired_or_revoked", "insufficient_permissions",
    "calendar_not_found", "calendar_not_selected", "event_not_found",
}


def _change_error(exc: GoogleCalendarError, action: str) -> BookingProviderError:
    status = str(getattr(exc, "status", "") or "google_api_error")
    # "unverified_response": Google answered, but its reply did not prove the
    # change (wrong event, cancelled, or not the requested times). The change
    # may or may not be on the calendar, exactly like an unreadable failure.
    if status in {"google_api_error", "unverified_response"}:
        return BookingProviderError(
            f"Google Calendar did not confirm the {action}; its outcome is unknown",
            code="BOOKING_OUTCOME_UNKNOWN", retryable=False,
        )
    code = status.upper() if status in _DEFINITE_GOOGLE_REFUSALS else "GOOGLE_REFUSED"
    return BookingProviderError(f"Google Calendar refused the {action}: {exc}", code=code)


def _disconnected(action: str) -> BookingProviderError:
    # The event lives in a Google calendar CARA can no longer reach: nothing
    # was sent, and the local mirror must not pretend it changed.
    return BookingProviderError(
        f"The Google Calendar holding this appointment is not connected, so the {action} "
        "was not made. Reconnect the calendar or change it there.",
        code="CALENDAR_DISCONNECTED",
    )


class GoogleCalendarAdapter(LocalCalendarAdapter):
    provider = "google_calendar"
    calendar_label = "the Google Calendar"

    write_back_supported = True
    # The Google event title would keep the old service while the local row
    # changed, so a same-booking service switch is refused, not half-applied.
    service_change_mode = "unsupported"
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
            if getattr(exc, "status", None) == "unverified_response":
                # The event may exist: durable unknown outcome, never a retry.
                raise BookingProviderError(
                    "Google Calendar did not confirm the new booking; its outcome is unknown",
                    code="BOOKING_OUTCOME_UNKNOWN", retryable=False,
                ) from exc
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
        # Free/busy is a real check against the connected calendar; record it
        # so the spa router can tell it apart from an unchecked default.
        return AvailabilityVerdict.ok(
            {
                "start": ctx.start.isoformat(),
                "duration_minutes": max(int((ctx.end - ctx.start).total_seconds() // 60), 1),
                "source": "google_freebusy",
            }
        )

    async def move_booking(self, booking, start, end):
        if not booking.external_id:
            return booking   # exists only in CARA: nothing to change in Google
        if not self.is_configured:
            raise _disconnected("move")
        try:
            await update_event(
                self.connection,
                booking.external_id,
                start=start,
                end=end,
                timezone_name=self.timezone_name,
            )
        except GoogleCalendarError as exc:
            logger.exception("Google Calendar update failed for %s", booking.external_id)
            raise _change_error(exc, "move") from exc
        return booking

    async def cancel_booking(self, booking) -> None:
        if not booking.external_id:
            return   # exists only in CARA: nothing to cancel in Google
        if not self.is_configured:
            raise _disconnected("cancellation")
        try:
            await delete_event(self.connection, booking.external_id)
        except GoogleCalendarError as exc:
            logger.exception("Google Calendar delete failed for %s", booking.external_id)
            raise _change_error(exc, "cancellation") from exc
