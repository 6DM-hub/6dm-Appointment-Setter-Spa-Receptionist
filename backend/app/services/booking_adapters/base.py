"""Booking strategy interface.

The local `Appointment` row is always written — it is what the dashboard, the
conflict check and the analytics endpoint read. An adapter sits *beside* that
write and owns two provider-specific concerns:

  1. whether a requested slot is bookable at all (business hours, provider-side
     staff availability);
  2. mirroring the booking into the external calendar the tenant actually runs
     their day from (Dominic's Google Calendar, a spa's Mindbody diary, ...).

Adapters therefore never touch the database. That keeps the transaction
boundary in one place — `app.services.appointment_booking_service` — and means
a provider outage degrades to "booked locally, not mirrored" instead of losing
the appointment.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.booking_adapters.customers import (
    CustomerLookupOutcome,
    CustomerLookupResult,
)
from app.services.booking_adapters.saved_payments import (
    SavedPaymentLookup,
    SavedPaymentLookupOutcome,
)


class BookingProviderError(RuntimeError):
    """Provider rejected or failed a calendar request.

    `code` carries the provider's own machine-readable error code (e.g.
    Square's `IDEMPOTENCY_KEY_REUSED`) when known, so callers can react to a
    specific failure instead of pattern-matching the human-readable message.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ProviderNotConfigured(BookingProviderError):
    """The tenant selected this provider but supplied no usable credentials."""


@dataclass(frozen=True)
class BookingContext:
    """Everything a provider needs to create or move a booking."""

    start: datetime
    end: datetime
    title: str
    customer_phone: str
    customer_name: str | None = None
    customer_email: str | None = None
    service_description: str | None = None
    notes: str | None = None
    booking_reference: str | None = None
    # The caller's explicit staff preference ("can I book with Sarah"), or None
    # when they have no preference ("I don't care who" / not mentioned). A
    # provider that supports staff assignment (Square) must filter availability
    # to this exact person when set, and never invent a substitute.
    preferred_staff: str | None = None
    # Dashboard staff who are allowed to perform this service, when the spa
    # has assigned services to people. Empty means no such restriction.
    # A caller-named preferred_staff still has to be one of these people.
    allowed_staff: tuple[str, ...] = ()
    # Set by a provider adapter's own resolution (e.g. Square's tenant-approved
    # menu -> catalog service variation) so `create_booking`/`move_booking` use
    # the exact same identifiers `check_availability` already validated,
    # instead of re-deriving them from the raw caller phrase a second time.
    service_variation_id: str | None = None
    service_variation_version: int | None = None
    # The exact provider slot the caller already heard read back to them
    # (start, team_member_id, service_variation_id/version, location,
    # duration), carried from `BookingDraft.selected_slot`. When present, a
    # provider that supports it (Square) must re-verify THIS EXACT
    # combination rather than running a fresh, unconstrained search that
    # could silently return a different therapist or service variation.
    selected_slot: dict[str, Any] | None = None
    # The person on the phone. Never the appointment guest.
    caller_name: str | None = None
    # The person the service is for, when that person is not the caller.
    guest_name: str | None = None
    # Authoritative provider id from the spa's configured team, when known.
    provider_id: str | None = None
    # Set only after a confident provider lookup. Create must not search-and-update
    # again when this is already known.
    external_customer_id: str | None = None


@dataclass(frozen=True)
class ExternalBooking:
    """Pointer to the mirrored booking in the provider's system."""

    provider: str
    external_id: str | None = None
    # Present only when the provider has a real customer directory record.
    external_customer_id: str | None = None
    customer_display_name: str | None = None
    customer_email: str | None = None
    customer_phone: str | None = None
    # "reused" when an existing provider customer was kept, "created" when
    # a new one was made after a confirmed zero-match search.
    customer_resolution: str | None = None


@dataclass(frozen=True)
class AvailabilityVerdict:
    available: bool
    # Phrased for the voice agent to paraphrase, e.g. "the spa is closed then".
    reason: str | None = None
    # Authoritative provider-confirmed slot data — populated ONLY from a real
    # provider round-trip (never computed or guessed) so the booking draft
    # can persist exactly what was offered, and so a hard runtime guard can
    # verify the agent never claims a time is available unless it is this.
    # Shape (provider-agnostic): start, location_id, team_member_id,
    # service_variation_id, service_variation_version, duration_minutes.
    slot: dict[str, Any] | None = None
    # Additional provider-confirmed slots from the SAME search response.
    # Used as alternatives when an exact requested time is unavailable.
    alternatives: tuple[dict[str, Any], ...] = ()

    @classmethod
    def ok(cls, slot: dict[str, Any] | None = None) -> "AvailabilityVerdict":
        return cls(available=True, slot=slot)

    @classmethod
    def no(
        cls,
        reason: str,
        alternatives: tuple[dict[str, Any], ...] = (),
    ) -> "AvailabilityVerdict":
        return cls(available=False, reason=reason, alternatives=alternatives)


class BookingAdapter(ABC):
    """Base strategy. Concrete adapters override only what they support."""

    #: Stored on Appointment.booking_provider.
    provider: str = "internal"
    #: Human label the voice agent uses, e.g. "Dominic's Google Calendar".
    calendar_label: str = "the calendar"
    #: Fallback appointment title when the caller never named a service.
    default_title: str = "Appointment"
    #: Used when the caller gives a start time but no duration.
    default_duration_minutes: int = 30
    #: Customer directory. Scheduling-only providers leave both false.
    supports_customer_lookup: bool = False
    supports_customer_creation: bool = False
    #: Saved payment methods. False until the adapter can actually list them.
    supports_saved_payment_method_lookup: bool = False
    #: Saving a card on file. False until the adapter can call the provider's
    #: card-create API. This does not charge the card.
    supports_save_card_on_file: bool = False

    async def booking_timezone_name(self) -> str | None:
        return None

    async def describe_location(self) -> dict[str, Any] | None:
        return None

    async def list_openings(
        self,
        ctx: BookingContext,
        range_start: datetime,
        range_end: datetime,
    ) -> list[dict[str, Any]]:
        """Provider-confirmed openings in [range_start, range_end). Empty if unsupported."""
        return []

    async def check_availability(self, ctx: BookingContext) -> AvailabilityVerdict:
        """Provider-side availability. Local double-booking is checked
        separately against the `appointments` table."""
        return AvailabilityVerdict.ok()

    @abstractmethod
    async def create_booking(self, ctx: BookingContext) -> ExternalBooking:
        """Mirror a new booking. Raise `BookingProviderError` on failure."""

    async def move_booking(
        self, booking: ExternalBooking, start: datetime, end: datetime
    ) -> ExternalBooking:
        """Reschedule a mirrored booking. Defaults to a no-op for providers
        with no write-back."""
        return booking

    async def cancel_booking(self, booking: ExternalBooking) -> None:
        """Cancel a mirrored booking. Defaults to a no-op."""
        return None

    async def find_customers_by_phone(self, phone: str) -> CustomerLookupResult:
        return CustomerLookupResult(outcome=CustomerLookupOutcome.UNSUPPORTED)

    async def get_customer(self, external_customer_id: str) -> CustomerLookupResult:
        return CustomerLookupResult(outcome=CustomerLookupOutcome.UNSUPPORTED)

    async def create_customer(self, ctx: BookingContext) -> CustomerLookupResult:
        return CustomerLookupResult(outcome=CustomerLookupOutcome.UNSUPPORTED)

    async def update_customer(
        self, external_customer_id: str, ctx: BookingContext
    ) -> CustomerLookupResult:
        """Explicit profile correction. Ordinary booking resolution must not call this."""
        return CustomerLookupResult(outcome=CustomerLookupOutcome.UNSUPPORTED)

    async def list_saved_payment_methods(self, external_customer_id: str) -> SavedPaymentLookup:
        return SavedPaymentLookup(outcome=SavedPaymentLookupOutcome.UNSUPPORTED)

    async def has_usable_saved_payment_method(
        self, external_customer_id: str
    ) -> SavedPaymentLookup:
        return SavedPaymentLookup(outcome=SavedPaymentLookupOutcome.UNSUPPORTED)


class LocalCalendarAdapter(BookingAdapter):
    """The always-available fallback: the booking lives only in our own
    database. Used when a tenant has picked a provider we cannot reach, so a
    misconfigured integration never costs the spa a booking."""

    provider = "internal"
    calendar_label = "the internal calendar"

    def __init__(
        self,
        calendar_label: str | None = None,
        default_title: str | None = None,
    ) -> None:
        if calendar_label:
            self.calendar_label = calendar_label
        if default_title:
            self.default_title = default_title

    async def create_booking(self, ctx: BookingContext) -> ExternalBooking:
        return ExternalBooking(provider=self.provider, external_id=None)
