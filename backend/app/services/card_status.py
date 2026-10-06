"""Decide an appointment's card_status after the booking itself has succeeded.

The provider remains the source of truth for saved cards. This module stores
only the status. It does not collect, save, or charge a card.
"""
from __future__ import annotations

import logging

from app.models.appointment import CardStatus
from app.models.spa_account import SpaAccount
from app.services.booking_adapters.base import BookingAdapter
from app.services.booking_adapters.saved_payments import SavedPaymentLookupOutcome
from app.services.spa_facts import payment_policy_of

logger = logging.getLogger(__name__)


def is_guest_booking(caller_name: str | None, guest_name: str | None) -> bool:
    """True when the service recipient is a different person from the caller."""
    guest = " ".join((guest_name or "").split())
    caller = " ".join((caller_name or "").split())
    if not guest:
        return False
    return guest.casefold() != caller.casefold()


async def card_status_for_booked_appointment(
    *,
    spa: SpaAccount | None,
    adapter: BookingAdapter,
    external_customer_id: str | None,
    caller_name: str | None,
    guest_name: str | None,
    guest_external_customer_id: str | None = None,
) -> CardStatus:
    """Status for a booking that has already succeeded.

    A guest booking never inherits the caller's saved card. Lookup errors
    become ``unknown``, which is not the same as "no card".
    """
    if spa is None or not payment_policy_of(spa)["card_required"]:
        return CardStatus.NOT_REQUIRED

    if is_guest_booking(caller_name, guest_name):
        # Only an independently resolved guest customer may be checked.
        # The caller's id is never substituted.
        customer_id = (guest_external_customer_id or "").strip() or None
        if customer_id is None:
            return CardStatus.UNKNOWN
    else:
        customer_id = (external_customer_id or "").strip() or None

    if not getattr(adapter, "supports_saved_payment_method_lookup", False):
        return CardStatus.NOT_SUPPORTED
    if not customer_id:
        return CardStatus.UNKNOWN

    try:
        lookup = await adapter.has_usable_saved_payment_method(customer_id)
    except Exception:
        logger.exception(
            "Saved-payment lookup failed for spa %s; card status left unknown",
            getattr(spa, "id", None),
        )
        return CardStatus.UNKNOWN

    outcome = getattr(lookup, "outcome", None)
    if outcome == SavedPaymentLookupOutcome.USABLE:
        return CardStatus.CARD_CONFIRMED
    if outcome == SavedPaymentLookupOutcome.NO_USABLE_METHOD:
        return CardStatus.PENDING_CARD
    if outcome == SavedPaymentLookupOutcome.UNSUPPORTED:
        return CardStatus.NOT_SUPPORTED
    return CardStatus.UNKNOWN
