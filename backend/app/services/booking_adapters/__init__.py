"""Multi-tenant booking adapters.

Two strategies, chosen by which product handled the call — never by user input:

  * `OutboundSalesAdapter` — 6DM Sales Agent, outbound B2B. Always Dominic's
    Google Calendar. A spa can never select it.
  * `SpaBookingAdapter`    — Spa Receptionist, inbound B2C. Routes to the
    tenant's configured provider (Mindbody, Mangomint, Square, Vagaro, Zenoti)
    with a Google/internal calendar fallback.
"""
from app.models.spa_account import SpaAccount
from app.services.booking_adapters.base import (
    AvailabilityVerdict,
    BookingAdapter,
    BookingContext,
    BookingProviderError,
    ExternalBooking,
    LocalCalendarAdapter,
    ProviderNotConfigured,
)
from app.services.booking_adapters.google_calendar import GoogleCalendarAdapter
from app.services.booking_adapters.outbound_sales import OutboundSalesAdapter
from app.services.booking_adapters.spa_router import SpaBookingAdapter


def get_booking_adapter(
    *, is_outbound_sales: bool, spa: SpaAccount | None
) -> BookingAdapter:
    """Pick the strategy for a call.

    `is_outbound_sales` comes from the call direction, which is set by the
    telephony layer rather than by anything the caller says — so an inbound spa
    conversation cannot talk its way onto Dominic's calendar.
    """
    if is_outbound_sales:
        return OutboundSalesAdapter()
    if spa is not None:
        return SpaBookingAdapter(spa)
    # Inbound call on a number no tenant claims: keep it local and labelled.
    return LocalCalendarAdapter(calendar_label="the internal calendar")


__all__ = [
    "AvailabilityVerdict",
    "BookingAdapter",
    "BookingContext",
    "BookingProviderError",
    "ExternalBooking",
    "GoogleCalendarAdapter",
    "LocalCalendarAdapter",
    "OutboundSalesAdapter",
    "ProviderNotConfigured",
    "SpaBookingAdapter",
    "get_booking_adapter",
]
