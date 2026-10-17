"""Bounded create recovery. Never retry a write with a new idempotency key."""
import asyncio
import copy
from datetime import datetime, timedelta
import httpx
from app.services.booking_adapters.base import BookingProviderError


def classify(error):
    code = str(getattr(error, "code", "") or "").upper()
    if code in {"UNAUTHORIZED", "FORBIDDEN", "INSUFFICIENT_SCOPES", "ACCESS_TOKEN_EXPIRED"}:
        return "permissions"
    if code in {"TIME_RANGE_UNAVAILABLE", "SLOT_UNAVAILABLE", "AVAILABILITY_UNAVAILABLE"}:
        return "unavailable"
    if code in {"CONFLICT", "VISIT_CONFLICT", "VERSION_MISMATCH", "IDEMPOTENCY_KEY_REUSED"}:
        return "conflict"
    if code in {"BOOKING_OUTCOME_UNKNOWN", "PARTIAL_BOOKING_UNRESOLVED"}:
        return "unknown_outcome"
    if isinstance(error, (asyncio.TimeoutError, httpx.TransportError)) or getattr(error, "retryable", False):
        return "temporary"
    return "configuration"


def same_booking(record, expected):
    try:
        instant = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
        keys = ("service_variation_id", "team_member_id", "duration_minutes", "service_variation_version")
        actual_segments, expected_segments = record["appointment_segments"], expected["appointment_segments"]
        return (record.get("id") and record.get("status") in {"ACCEPTED", "PENDING"}
                and all(record.get(k) == expected.get(k) for k in ("location_id", "customer_id"))
                and instant(record["start_at"]) == instant(expected["start_at"])
                and len(actual_segments) == len(expected_segments)
                and all(all(a.get(k) == b.get(k) for k in keys) for a, b in zip(actual_segments, expected_segments)))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


async def reconcile(delegate, payload):
    expected = payload["booking"]
    start = datetime.fromisoformat(expected["start_at"].replace("Z", "+00:00"))
    params = {"location_id": expected["location_id"], "customer_id": expected["customer_id"],
              "start_at_min": (start - timedelta(seconds=1)).isoformat(),
              "start_at_max": (start + timedelta(seconds=1)).isoformat(), "limit": 100}
    matches = []
    for _ in range(10):
        data = await delegate._request("GET", "/v2/bookings", params=params)
        if not isinstance(data, dict) or data.get("errors") or not isinstance(data.get("bookings", []), list):
            raise BookingProviderError("Booking readback failed.", code="BOOKING_OUTCOME_UNKNOWN")
        for row in data.get("bookings", []):
            if row.get("status") == "ACCEPTED" and same_booking(row, expected):
                matches.append(row)
        if not data.get("cursor"):
            if len(matches) > 1:
                raise BookingProviderError("Multiple matching bookings require staff review.", code="BOOKING_OUTCOME_UNKNOWN")
            return matches[0] if matches else None
        params["cursor"] = data["cursor"]
    raise BookingProviderError("Booking readback exceeded its page limit.", code="BOOKING_OUTCOME_UNKNOWN")


async def create_with_recovery(delegate, payload):
    payload = copy.deepcopy(payload)
    if not payload.get("idempotency_key"):
        raise BookingProviderError("A stable booking key is required.", code="BOOKING_CONFIGURATION")
    from app.services.square_pending_create import before_create
    await before_create(delegate, payload)
    for attempt in range(2):
        try:
            return await delegate._request("POST", "/v2/bookings", json=copy.deepcopy(payload))
        except (BookingProviderError, httpx.TransportError, asyncio.TimeoutError) as error:
            if classify(error) in {"unknown_outcome", "temporary"}:
                # _request maps lost booking-write replies to unknown, not
                # temporary. Square's same-key replay identifies this exact
                # operation; a coincidentally matching list row does not.
                if attempt:
                    raise
                try:
                    replay = await delegate._request("POST", "/v2/bookings", json=copy.deepcopy(payload))
                    booking = replay.get("booking") if isinstance(replay, dict) else None
                    if not same_booking(booking, payload["booking"]):
                        raise ValueError("Idempotent replay did not match the booking")
                    booking_id = booking["id"]
                    # A replay can contain the original ACCEPTED response even
                    # after cancellation: verify today's provider record too.
                    current = await delegate._request("GET", f"/v2/bookings/{booking_id}")
                    current_booking = current.get("booking") if isinstance(current, dict) else None
                    if (not same_booking(current_booking, payload["booking"])
                            or current_booking["id"] != booking_id):
                        raise ValueError("Current booking did not match the replay")
                    return {"booking": current_booking}
                except (BookingProviderError, httpx.TransportError, asyncio.TimeoutError, ValueError, TypeError) as recovery_error:
                    raise BookingProviderError(
                        "The booking outcome cannot be verified; staff must check Square before retrying.",
                        code="BOOKING_OUTCOME_UNKNOWN",
                    ) from recovery_error
            raise
