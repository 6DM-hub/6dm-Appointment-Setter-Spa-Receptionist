"""Regression coverage for the real `IDEMPOTENCY_KEY_REUSED` booking failure.

Trace (from a live call): `create_booking` -> `_get_or_create_customer` ->
POST `/v2/customers` raised `BookingProviderError` with Square's
`IDEMPOTENCY_KEY_REUSED` error code. The customer-create idempotency key is a
pure function of (phone, given_name, family_name, email), so a *genuine*
retry with the same caller always regenerates the same key AND the same
payload. Square only rejects a key when it already has a *different* payload
on file for it — almost always because an earlier attempt for this exact
caller actually succeeded at Square, but this backend gave up waiting for the
response (see the `CALENDAR_TIMEOUT_SECONDS` vs. per-request `httpx` timeout
mismatch fixed alongside this) and, on retry, the customer-search index had
not yet caught up when we re-checked it before creating again.

The fix: on `IDEMPOTENCY_KEY_REUSED` specifically, `_get_or_create_customer`
re-searches by phone (Square is the only source of truth for whether the
customer already exists) instead of surfacing a raw provider failure that a
higher layer could misreport as "the slot is no longer available" or blindly
retry into a duplicate customer.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import BookingProvider
from app.services.booking_adapters import BookingContext, SpaBookingAdapter
from app.services.booking_adapters.base import BookingProviderError
from tests.conftest import make_spa


def _ctx(**overrides) -> BookingContext:
    start = overrides.pop("start", datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc))
    end = overrides.pop("end", start + timedelta(hours=1))
    defaults = dict(
        start=start,
        end=end,
        title="Deep tissue massage",
        customer_phone="+15550001",
        customer_name="Green",
        service_description="Deep tissue massage",
    )
    defaults.update(overrides)
    return BookingContext(**defaults)


class _FakeTransport:
    """Same routing convention as `test_square_timezone_and_staff.py`'s
    `_FakeSquareTransport`, plus scripted failures for specific paths."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.catalog_items: list[dict] = []
        self.availabilities: list[dict] = []
        self.team_members: list[dict] = []
        # Each element is either a list of customers to return, or an
        # Exception instance to raise, consumed in order per search call.
        self.customer_search_results: list[object] = [[]]
        self.create_customer_results: list[object] = [{"customer": {"id": "cust_new"}}]
        self.created_booking: dict = {"id": "sq_book_1", "status": "ACCEPTED"}

    async def __call__(self, method: str, path: str, *, json: dict | None = None) -> dict:
        self.calls.append((method, path, json or {}))

        if str(path).startswith("/v2/locations/"):
            loc_id = path.rsplit("/", 1)[-1]
            return {
                "location": {
                    "id": loc_id,
                    "name": "Test Square Location",
                    "timezone": "America/Chicago",
                    "status": "ACTIVE",
                }
            }
        if path == "/v2/catalog/search-catalog-items":
            return {"items": self.catalog_items}
        if path == "/v2/bookings/availability/search":
            return {"availabilities": self.availabilities}
        if path == "/v2/team-members/search":
            return {"team_members": self.team_members}
        if path == "/v2/customers/search":
            result = self.customer_search_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return {"customers": result}
        if path == "/v2/customers" and method == "POST":
            result = self.create_customer_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        if path.startswith("/v2/customers/") and method == "PUT":
            return {"customer": {"id": path.rsplit("/", 1)[-1]}}
        if path == "/v2/bookings":
            return {"booking": self.created_booking}
        raise AssertionError(f"unexpected Square path called: {path}")

    def calls_to(self, path: str) -> list[dict]:
        return [payload for _, p, payload in self.calls if p == path]


def _adapter(monkeypatch, transport: _FakeTransport):
    spa = make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "t", "location_id": "loc_123"},
    )
    adapter = SpaBookingAdapter(spa)
    monkeypatch.setattr(adapter.delegate, "_request", transport)
    return adapter


@pytest.mark.asyncio
async def test_idempotency_key_reused_recovers_the_already_created_customer(monkeypatch):
    """The exact reported failure: Square rejects the create with
    IDEMPOTENCY_KEY_REUSED. The fix must recover by re-searching for the
    customer that already exists, not raise a raw provider error, and must
    NOT attempt a second create (that would risk a duplicate customer)."""
    transport = _FakeTransport()
    # First search (before create): nothing found yet.
    # Second search (recovery after IDEMPOTENCY_KEY_REUSED): found.
    transport.customer_search_results = [
        [],
        [{"id": "cust_already_created", "version": 1}],
    ]
    transport.create_customer_results = [
        BookingProviderError(
            "Square API request failed: IDEMPOTENCY_KEY_REUSED "
            "[idempotency_key]: The idempotency key can only be retried "
            "with the same request data.",
            code="IDEMPOTENCY_KEY_REUSED",
        ),
    ]
    adapter = _adapter(monkeypatch, transport)

    customer_id = await adapter.delegate._get_or_create_customer(_ctx())

    assert customer_id == "cust_already_created"
    # Exactly one create attempt — recovery must never retry the create.
    assert len(transport.calls_to("/v2/customers")) == 1
    assert len(transport.calls_to("/v2/customers/search")) == 2


@pytest.mark.asyncio
async def test_idempotency_key_reused_with_no_recoverable_customer_raises_classified_error(monkeypatch):
    """If the recovery search genuinely finds nobody, the failure must be
    reported as a classified provider/idempotency conflict — never silently
    swallowed, and never reported as a routine "slot unavailable"."""
    transport = _FakeTransport()
    transport.customer_search_results = [[], [], [], []]
    transport.create_customer_results = [
        BookingProviderError(
            "Square API request failed: IDEMPOTENCY_KEY_REUSED",
            code="IDEMPOTENCY_KEY_REUSED",
        ),
    ]
    adapter = _adapter(monkeypatch, transport)

    with pytest.raises(BookingProviderError) as exc_info:
        await adapter.delegate._get_or_create_customer(_ctx())

    assert exc_info.value.code == "CUSTOMER_IDEMPOTENCY_CONFLICT"
    # Still never retried the create itself.
    assert len(transport.calls_to("/v2/customers")) == 1


@pytest.mark.asyncio
async def test_other_square_errors_on_customer_create_are_not_treated_as_idempotency_conflicts(monkeypatch):
    """A genuine, unrelated Square error on customer creation must propagate
    as-is — the recovery path is specific to IDEMPOTENCY_KEY_REUSED."""
    transport = _FakeTransport()
    transport.customer_search_results = [[]]
    transport.create_customer_results = [
        BookingProviderError(
            "Square API request failed: HTTP 500",
            code=None,
        ),
    ]
    adapter = _adapter(monkeypatch, transport)

    with pytest.raises(BookingProviderError) as exc_info:
        await adapter.delegate._get_or_create_customer(_ctx())

    assert exc_info.value.code != "CUSTOMER_IDEMPOTENCY_CONFLICT"
    # No recovery search was attempted for an unrelated error.
    assert len(transport.calls_to("/v2/customers/search")) == 1


@pytest.mark.asyncio
async def test_full_booking_succeeds_end_to_end_after_idempotency_recovery(monkeypatch):
    """Availability -> customer idempotency conflict -> recovery -> booking
    create, all the way to a valid Square booking id. Proves the fix does not
    just patch the customer call in isolation but lets a real booking
    complete."""
    transport = _FakeTransport()
    transport.catalog_items = [
        {
            "item_data": {
                "name": "Deep tissue massage",
                "variations": [
                    {"id": "var1", "version": 4, "item_variation_data": {"name": "Regular"}}
                ],
            }
        }
    ]
    transport.availabilities = [
        {
            "start_at": "2026-09-24T19:00:00Z",
            "location_id": "loc_123",
            "appointment_segments": [
                {
                    "team_member_id": "TM1",
                    "service_variation_id": "var1",
                    "service_variation_version": 4,
                    "duration_minutes": 60,
                }
            ],
        }
    ]
    transport.customer_search_results = [
        [],
        [{"id": "cust_already_created", "version": 1}],
    ]
    transport.create_customer_results = [
        BookingProviderError(
            "Square API request failed: IDEMPOTENCY_KEY_REUSED",
            code="IDEMPOTENCY_KEY_REUSED",
        ),
    ]
    transport.created_booking = {"id": "sq_book_42", "status": "ACCEPTED"}
    adapter = _adapter(monkeypatch, transport)

    result = await adapter.create_booking(_ctx())

    assert result.external_id == "sq_book_42"
    booking_call = transport.calls_to("/v2/bookings")[0]
    assert booking_call["booking"]["customer_id"] == "cust_already_created"
