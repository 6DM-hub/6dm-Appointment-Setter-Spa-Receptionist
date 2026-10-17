"""Per-task write-ahead hook and explicit Square create reconciliation.

Recording belongs to the booking transaction owner. The provider calls the
hook after constructing its final payload, before POST. ContextVar isolation
keeps concurrent tenants and calls from sharing callbacks.
"""
import copy
from contextlib import contextmanager
from contextvars import ContextVar

from app.services.booking_adapters.base import BookingProviderError
from app.services.booking_config import decrypt_config
from app.services.square_booking_recovery import same_booking

_recorder = ContextVar("square_create_recorder", default=None)


@contextmanager
def recording_creates(callback):
    token = _recorder.set(callback)
    try:
        yield
    finally:
        _recorder.reset(token)


async def before_create(delegate, payload):
    callback = _recorder.get()
    if callback is not None:
        await callback(delegate, copy.deepcopy(payload))


async def verify_pending_square(db, tenant_id, details):
    """GET a known ID, otherwise replay the exact saved POST/key, then GET.

    The replay is an idempotent write, NOT a read-only lookup. Never list-match
    a booking, regenerate a payload, or accept PENDING as confirmed.
    """
    from app.models import SpaAccount
    from app.services.booking_adapters.spa_router import SpaBookingAdapter
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    spa = (await db.execute(select(SpaAccount).where(SpaAccount.id == tenant_id)
                            .options(selectinload(SpaAccount.google_calendar_connection)))).scalar_one_or_none()
    if spa is None:
        raise BookingProviderError("Business no longer exists", code="BOOKING_OUTCOME_UNKNOWN")
    adapter = SpaBookingAdapter(spa)
    delegate = adapter.delegate
    payload = decrypt_config(details.get("square_request"))
    if (getattr(delegate, "provider", None) != "square"
            or getattr(delegate, "environment", None) != details.get("square_environment")
            or getattr(delegate, "api_version", None) != details.get("square_api_version")
            or not isinstance(payload.get("booking"), dict)
            or payload["booking"].get("location_id") != getattr(delegate, "location_id", None)
            or not payload.get("idempotency_key")):
        raise BookingProviderError("Saved Square request or configuration cannot be verified",
                                   code="BOOKING_OUTCOME_UNKNOWN")
    expected = payload["booking"]
    booking_id = details.get("external_booking_id") if details.get("provider_success_verified") else None
    try:
        if not booking_id:
            replay = await delegate._request("POST", "/v2/bookings", json=copy.deepcopy(payload))
            record = replay.get("booking") if isinstance(replay, dict) else None
            if not same_booking(record, expected):
                raise ValueError("Saved request replay is not a verified active request")
            booking_id = record["id"]
        current = await delegate._request("GET", f"/v2/bookings/{booking_id}")
        record = current.get("booking") if isinstance(current, dict) else None
        if not same_booking(record, expected) or record["id"] != booking_id:
            raise ValueError("Current booking differs from the saved request")
        return booking_id, record["status"]
    except Exception as exc:
        raise BookingProviderError("Square recovery remains unverified", code="BOOKING_OUTCOME_UNKNOWN") from exc
