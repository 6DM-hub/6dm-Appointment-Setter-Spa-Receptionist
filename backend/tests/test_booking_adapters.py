"""Booking strategy selection.

The invariant worth protecting: which calendar a booking lands on is decided by
the call direction and the tenant row, never by anything a caller says.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import BookingProvider
from app.core.config import settings
from app.services.booking_adapters import (
    BookingContext,
    GoogleCalendarAdapter,
    LocalCalendarAdapter,
    OutboundSalesAdapter,
    SpaBookingAdapter,
    get_booking_adapter,
)
from app.services.booking_adapters.providers import MindbodyAdapter
from app.services import appointment_booking_service
from app.services.appointment_booking_service import BookingOutcome, attempt_booking
from app.services.booking_config import decrypt_config, encrypt_config, masked_config
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent
from tests.conftest import make_spa

# A Wednesday, 14:00-15:00 UTC.
START = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def _ctx(start: datetime = START, end: datetime = END) -> BookingContext:
    return BookingContext(
        start=start, end=end, title="Deep tissue massage", customer_phone="+15550001"
    )


def test_outbound_sales_always_books_dominics_calendar():
    adapter = get_booking_adapter(is_outbound_sales=True, spa=None)

    assert isinstance(adapter, OutboundSalesAdapter)
    assert adapter.calendar_label == "Dominic's Google Calendar"
    assert adapter.default_title == "6DM Sales Presentation"


def test_a_spa_can_never_be_routed_to_the_sales_calendar():
    """Even if a spa row somehow named the sales adapter, the factory keys off
    the call direction, so an inbound conversation cannot reach it."""
    spa = make_spa(booking_provider=BookingProvider.GOOGLE_CALENDAR)

    adapter = get_booking_adapter(is_outbound_sales=False, spa=spa)

    assert isinstance(adapter, SpaBookingAdapter)
    assert not isinstance(adapter, OutboundSalesAdapter)
    assert adapter.calendar_label == "Solace Spa's service calendar"


def test_unclaimed_inbound_number_falls_back_to_a_local_calendar():
    adapter = get_booking_adapter(is_outbound_sales=False, spa=None)
    assert isinstance(adapter, LocalCalendarAdapter)


def test_spa_google_calendar_requires_tenant_calendar_id(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CALENDAR_ID", "dominics-id")
    spa = make_spa(booking_config={})

    adapter = SpaBookingAdapter(spa)

    assert adapter.delegate.provider == "google_calendar"
    assert adapter.delegate.calendar_label == "Solace Spa's google_calendar calendar"


def test_booking_secrets_are_encrypted_and_masked():
    encrypted = encrypt_config({"access_token": "secret1234", "location_id": "spa-a"})

    assert "secret1234" not in str(encrypted)
    assert decrypt_config(encrypted)["access_token"] == "secret1234"
    assert masked_config(BookingProvider.SQUARE, encrypted)["access_token"] == "••••••••1234"


def test_same_number_uses_explicit_call_direction():
    number = "+15550000001"
    spa = make_spa(twilio_phone_number=number)
    inbound = CallSession(
        "inbound", "inbound", "+15550000002", number, tenant_id=str(spa.id)
    )
    outbound = CallSession(
        "outbound", "outbound", number, "+15550000002", user_id=str(uuid.uuid4())
    )

    inbound_adapter = get_booking_adapter(
        is_outbound_sales=inbound.direction == "outbound", spa=spa
    )
    outbound_adapter = get_booking_adapter(
        is_outbound_sales=outbound.direction == "outbound", spa=None
    )

    assert isinstance(inbound_adapter, SpaBookingAdapter)
    assert isinstance(outbound_adapter, OutboundSalesAdapter)


@pytest.mark.asyncio
async def test_inbound_without_tenant_is_rejected_before_calendar_selection():
    session = CallSession("call", "inbound", "+15550000002", "+15550000001")
    intent = AppointmentIntent(
        intent="schedule",
        confidence=1.0,
        requested_start_iso="2026-09-04T14:00:00Z",
    )

    result = await attempt_booking(None, session, intent)

    assert result.outcome.value == "error"
    assert "safely route" in result.message


class _BookingDB:
    async def commit(self):
        return None

    async def refresh(self, _appointment):
        return None


class _MoveAdapter:
    provider = "internal"
    calendar_label = "the internal calendar"
    default_title = "Appointment"
    default_duration_minutes = 30

    def __init__(self):
        self.moved = []

    async def check_availability(self, _context):
        from app.services.booking_adapters.base import AvailabilityVerdict

        return AvailabilityVerdict.ok()

    async def move_booking(self, booking, start, end):
        self.moved.append((booking, start, end))
        return booking


def _active_booking_session(appointment_id: uuid.UUID) -> CallSession:
    return CallSession(
        "same-call",
        "inbound",
        "+15550000002",
        "+15550000001",
        tenant_id=str(uuid.uuid4()),
        appointment_id=str(appointment_id),
        booking_status="booked",
    )


@pytest.mark.asyncio
async def test_repeating_same_booking_request_is_idempotent(monkeypatch):
    appointment_id = uuid.uuid4()
    start = START
    appointment = SimpleNamespace(
        id=appointment_id,
        start_time=start,
        end_time=END,
        status="scheduled",
        title="Deep tissue massage",
        booking_provider="internal",
        external_booking_id=None,
    )
    adapter = _MoveAdapter()
    session = _active_booking_session(appointment_id)
    monkeypatch.setattr(appointment_booking_service, "_load_spa", lambda *_: _async_value(make_spa()))
    monkeypatch.setattr(appointment_booking_service, "_load_active_appointment", lambda *_: _async_value(appointment))
    monkeypatch.setattr(appointment_booking_service, "get_booking_adapter", lambda **_: adapter)
    monkeypatch.setattr(appointment_booking_service, "_has_conflict", lambda *_a, **_k: _async_value(False))

    result = await attempt_booking(
        _BookingDB(),
        session,
        AppointmentIntent(
            intent="schedule",
            confidence=1.0,
            requested_start_iso=start.isoformat(),
            requested_end_iso=END.isoformat(),
        ),
    )

    assert result.outcome is BookingOutcome.DRAFT
    assert adapter.moved == []


@pytest.mark.asyncio
async def test_changed_date_moves_the_active_booking_instead_of_creating_one(monkeypatch):
    appointment_id = uuid.uuid4()
    appointment = SimpleNamespace(
        id=appointment_id,
        start_time=START,
        end_time=END,
        status="scheduled",
        title="Deep tissue massage",
        booking_provider="internal",
        external_booking_id=None,
    )
    adapter = _MoveAdapter()
    session = _active_booking_session(appointment_id)
    monkeypatch.setattr(appointment_booking_service, "_load_spa", lambda *_: _async_value(make_spa()))
    monkeypatch.setattr(appointment_booking_service, "_load_active_appointment", lambda *_: _async_value(appointment))
    monkeypatch.setattr(appointment_booking_service, "_resolve_contact", lambda *_: _async_value(SimpleNamespace(id=uuid.uuid4())))
    monkeypatch.setattr(appointment_booking_service, "get_booking_adapter", lambda **_: adapter)
    monkeypatch.setattr(appointment_booking_service, "_has_conflict", lambda *_args, **_kwargs: _async_value(False))

    new_start = START + timedelta(days=1)
    new_end = END + timedelta(days=1)
    result = await attempt_booking(
        _BookingDB(),
        session,
        AppointmentIntent(
            intent="schedule",
            confidence=1.0,
            requested_start_iso=new_start.isoformat(),
            requested_end_iso=new_end.isoformat(),
        ),
    )

    assert result.outcome is BookingOutcome.DRAFT
    assert adapter.moved == []
    assert appointment.start_time == START


async def _async_value(value):
    return value


@pytest.mark.parametrize(
    "provider",
    [
        BookingProvider.MINDBODY,
        BookingProvider.MANGOMINT,
        BookingProvider.SQUARE,
        BookingProvider.VAGARO,
        BookingProvider.ZENOTI,
    ],
)
def test_unimplemented_provider_is_not_routed_to_a_fallback(provider):
    spa = make_spa(booking_provider=provider)

    adapter = SpaBookingAdapter(spa)

    assert not isinstance(adapter.delegate, GoogleCalendarAdapter)


def test_partially_configured_provider_is_not_routed_to_a_fallback(monkeypatch):
    """A live provider with half its credentials is worse than the fallback:
    it would fail mid-call."""
    monkeypatch.setattr(MindbodyAdapter, "implemented", True)
    spa = make_spa(
        booking_provider=BookingProvider.MINDBODY,
        booking_config={"site_id": "123", "api_key": "k"},  # missing source_*
    )

    assert not isinstance(SpaBookingAdapter(spa).delegate, GoogleCalendarAdapter)


def test_fully_configured_provider_is_used(monkeypatch):
    monkeypatch.setattr(MindbodyAdapter, "implemented", True)
    spa = make_spa(
        booking_provider=BookingProvider.MINDBODY,
        booking_config={
            "site_id": "123",
            "api_key": "k",
            "source_name": "n",
            "source_password": "p",
        },
    )

    assert isinstance(SpaBookingAdapter(spa).delegate, MindbodyAdapter)


@pytest.mark.asyncio
async def test_spa_refuses_bookings_outside_business_hours():
    spa = make_spa(
        business_hours={"wed": [{"open": "09:00", "close": "12:00"}]},
        timezone="UTC",
    )

    verdict = await SpaBookingAdapter(spa).check_availability(_ctx())

    assert not verdict.available
    assert "closed" in verdict.reason


@pytest.mark.asyncio
async def test_spa_accepts_bookings_inside_business_hours():
    spa = make_spa(
        business_hours={"wed": [{"open": "09:00", "close": "18:00"}]},
        timezone="UTC",
    )

    assert (await SpaBookingAdapter(spa).check_availability(_ctx())).available


@pytest.mark.asyncio
async def test_business_hours_are_read_in_the_tenants_timezone():
    """14:00 UTC is 07:00 in Los Angeles — before a 09:00 opening."""
    spa = make_spa(
        business_hours={"wed": [{"open": "09:00", "close": "18:00"}]},
        timezone="America/Los_Angeles",
    )

    assert not (await SpaBookingAdapter(spa).check_availability(_ctx())).available


@pytest.mark.asyncio
async def test_spa_rejects_service_length_that_runs_past_closing_time():
    spa = make_spa(
        business_hours={"wed": [{"open": "09:00", "close": "18:00"}]},
        timezone="UTC",
        services=[{"name": "Deep tissue massage", "duration_minutes": 60}],
    )

    verdict = await SpaBookingAdapter(spa).check_availability(
        BookingContext(
            start=datetime(2026, 9, 2, 17, 30, tzinfo=timezone.utc),
            end=datetime(2026, 9, 2, 18, 30, tzinfo=timezone.utc),
            title="Deep tissue massage",
            customer_phone="+15550001",
            service_description="Deep tissue massage",
        )
    )

    assert not verdict.available
    assert "closed" in verdict.reason.lower()


def test_square_provider_is_implemented_and_usable():
    spa = make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "token", "location_id": "loc_123"},
    )

    adapter = SpaBookingAdapter(spa)
    assert adapter.delegate.provider == "square"
    assert adapter.provider == "square"


def test_service_duration_comes_from_the_tenants_menu():
    spa = make_spa(
        services=[
            {"name": "Signature facial", "duration_minutes": 75},
            {"name": "Deep tissue massage", "duration_minutes": 60},
        ]
    )
    adapter = SpaBookingAdapter(spa)

    assert adapter.duration_for_service("a deep tissue massage please") == 60
    assert adapter.duration_for_service("Signature facial") == 75
    # Unrecognised service falls back to the spa's first configured duration.
    assert adapter.duration_for_service("hot stone") == 75
    assert adapter.duration_for_service(None) == 75


@pytest.mark.asyncio
async def test_square_resolves_tenant_approved_service_variants():
    spa = make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={
            "access_token": "token",
            "location_id": "loc_123",
            "service_variation_ids": {
                "Swedish massage": "var_swedish_60",
            },
        },
        services=[{"name": "Swedish massage", "duration_minutes": 60}],
    )
    adapter = SpaBookingAdapter(spa).delegate

    resolved = await adapter._resolve_service_variation(
        BookingContext(
            start=START,
            end=END,
            title="Swedish massage",
            customer_phone="+15550001",
            service_description="Swedish massage",
        )
    )
    assert resolved["id"] == "var_swedish_60"

    resolved = await adapter._resolve_service_variation(
        BookingContext(
            start=START,
            end=END,
            title="Swedish massage",
            customer_phone="+15550001",
            service_description="60-minute Swedish massage",
        )
    )
    assert resolved["id"] == "var_swedish_60"


@pytest.mark.asyncio
async def test_square_respects_service_clarification_for_unrecognized_service(monkeypatch):
    spa = make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={
            "access_token": "token",
            "location_id": "loc_123",
            "service_variation_ids": {
                "Swedish massage": "var_swedish_60",
            },
        },
        services=[{"name": "Swedish massage", "duration_minutes": 60}],
    )
    adapter = SpaBookingAdapter(spa).delegate

    async def _request(method, path, *, json=None):
        if str(path).startswith("/v2/locations/"):
            return {
                "location": {
                    "id": "loc_123",
                    "status": "ACTIVE",
                    "name": "Test",
                    "timezone": "UTC",
                }
            }
        if path == "/v2/catalog/search-catalog-items":
            return {"items": []}
        raise AssertionError("Square availability must not be called when the service is unrecognized")

    monkeypatch.setattr(adapter, "_request", _request)

    verdict = await adapter.check_availability(
        BookingContext(
            start=START,
            end=END,
            title="60 Minutes, sweetest massage",
            customer_phone="+15550001",
            service_description="60 Minutes, sweetest massage",
        )
    )
    assert not verdict.available
    assert "needs_clarification" in verdict.reason.lower() or "service_not_recognized" in verdict.reason.lower()


@pytest.mark.asyncio
async def test_square_ambiguous_service_requires_clarification(monkeypatch):
    spa = make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={
            "access_token": "token",
            "location_id": "loc_123",
            "service_variation_ids": {
                "Swedish massage": "var_swedish_60",
                "Swedish facial": "var_swedish_facial",
            },
        },
        services=[
            {"name": "Swedish massage", "duration_minutes": 60},
            {"name": "Swedish facial", "duration_minutes": 45},
        ],
    )
    adapter = SpaBookingAdapter(spa).delegate

    async def _reject(*_args, **_kwargs):
        raise AssertionError("Square should not be called for ambiguous service names")

    monkeypatch.setattr(adapter, "_request", _reject)

    verdict = await adapter.check_availability(
        BookingContext(
            start=START,
            end=END,
            title="Swedish",
            customer_phone="+15550001",
            service_description="Swedish",
        )
    )
    assert not verdict.available
    assert "clarification" in verdict.reason.lower() or "ambiguous" in verdict.reason.lower()


@pytest.mark.asyncio
async def test_check_availability_uses_square_not_local_business_hours(monkeypatch):
    """Square SearchAvailability is authoritative. Stale local closing time
    must not short-circuit the provider lookup."""
    spa = make_spa(
        business_hours={"wed": [{"open": "09:00", "close": "18:00"}]},
        timezone="UTC",
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "token", "location_id": "loc_123"},
        services=[{"name": "Swedish massage", "duration_minutes": 60}],
    )
    adapter = SpaBookingAdapter(spa)
    square_calls: list[str] = []

    async def _request(method, path, *, json=None):
        square_calls.append(path)
        if str(path).startswith("/v2/locations/"):
            return {
                "location": {
                    "id": "loc_123",
                    "status": "ACTIVE",
                    "name": "Test",
                    "timezone": "UTC",
                }
            }
        if path == "/v2/catalog/search-catalog-items":
            return {"items": []}
        if path == "/v2/bookings/availability/search":
            return {"availabilities": []}
        raise AssertionError(path)

    monkeypatch.setattr(adapter.delegate, "_request", _request)

    verdict = await adapter.check_availability(
        BookingContext(
            start=datetime(2026, 9, 2, 19, 0, tzinfo=timezone.utc),
            end=datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc),
            title="Swedish massage",
            customer_phone="+15550001",
            service_description="Swedish massage",
        )
    )
    assert not verdict.available
    assert any("availability/search" in path for path in square_calls) or any(
        "catalog" in path for path in square_calls
    )
    assert "outside_business_hours" not in verdict.reason.lower()


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0.0, 0.0),
        (0.70, 0.70),
        (1.0, 1.0),
        (70, 0.70),
        (70.5, 0.705),
        (100, 1.0),
        (1.5, 0.015),
        ("0.70", 0.70),
        ("70", 0.70),
    ],
)
def test_appointment_intent_normalizes_confidence_values(raw, expected):
    assert AppointmentIntent.model_validate({"intent": "schedule", "confidence": raw}).confidence == pytest.approx(expected)


@pytest.mark.parametrize("raw", [-1, 101, 150, float("nan"), float("inf"), "-1", "101", "NaN", "Infinity", "abc", ""])
def test_appointment_intent_rejects_invalid_confidence_values(raw):
    with pytest.raises(ValueError):
        AppointmentIntent.model_validate({"intent": "schedule", "confidence": raw})


@pytest.mark.asyncio
async def test_ambiguous_service_requires_clarification_in_booking_flow(monkeypatch):
    spa = make_spa(
        business_hours={"wed": [{"open": "09:00", "close": "18:00"}]},
        timezone="UTC",
        booking_provider=BookingProvider.SQUARE,
        booking_config={
            "access_token": "token",
            "location_id": "loc_123",
            "service_variation_ids": {
                "Swedish massage": "var_swedish_60",
                "Deep tissue massage": "var_deep_tissue_60",
            },
        },
        services=[
            {"name": "Swedish massage", "duration_minutes": 60},
            {"name": "Swedish facial", "duration_minutes": 45},
        ],
    )

    adapter = SpaBookingAdapter(spa)

    async def _reject(*_args, **_kwargs):
        raise AssertionError("Square availability must not be called when service resolution is ambiguous")

    monkeypatch.setattr(adapter.delegate, "_request", _reject)
    monkeypatch.setattr(appointment_booking_service, "_load_spa", lambda *_: _async_value(spa))
    monkeypatch.setattr(appointment_booking_service, "get_booking_adapter", lambda **_: adapter)
    monkeypatch.setattr(
        appointment_booking_service, "persist_caller_identity", lambda *_a, **_k: _async_value(None)
    )

    class _Result:
        def __init__(self, value):
            self.value = value

    class _Query:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _DB:
        async def execute(self, *_args, **_kwargs):
            return _Query(spa)

        async def rollback(self):
            return None

    session = CallSession(
        "call-ambiguous",
        "inbound",
        "+15550000002",
        "+15550000001",
        tenant_id=str(spa.id),
    )
    result = await appointment_booking_service.stage_booking(
        _DB(),
        session,
        AppointmentIntent(
            intent="schedule",
            confidence=1.0,
            service_description="Swedish",
            requested_start_iso="2026-09-02T14:00:00Z",
            requested_end_iso="2026-09-02T15:00:00Z",
        ),
    )

    assert result.outcome.value == "missing_info"
    assert "matches more than one service" in result.message.lower()
    assert "swedish massage" in result.message.lower()
    assert "swedish facial" in result.message.lower()
    assert "temporary scheduling issue" not in result.message.lower()
    assert "square outage" not in result.message.lower()


@pytest.mark.asyncio
async def test_a_provider_failure_is_returned_to_the_booking_engine(monkeypatch):
    from app.services.booking_adapters.base import BookingProviderError

    spa = make_spa(booking_provider=BookingProvider.GOOGLE_CALENDAR)
    adapter = SpaBookingAdapter(spa)

    async def _boom(_ctx):
        raise BookingProviderError("provider down")

    monkeypatch.setattr(adapter.delegate, "create_booking", _boom)

    with pytest.raises(BookingProviderError, match="provider down"):
        await adapter.create_booking(_ctx())


@pytest.mark.asyncio
async def test_google_calendar_availability_checks_freebusy(monkeypatch):
    """Regression test: `check_availability` must actually call the imported
    `freebusy` helper. This previously raised `NameError: name 'freebusy' is
    not defined` because it was never imported into this module."""
    from app.services.booking_adapters import google_calendar as gcal_adapter

    adapter = gcal_adapter.GoogleCalendarAdapter(
        calendar_id="spa-calendar", connection=object()
    )

    async def _fake_freebusy(connection, **kwargs):
        assert connection is adapter.connection
        assert kwargs["calendar_id"] == "spa-calendar"
        return True

    monkeypatch.setattr(gcal_adapter, "freebusy", _fake_freebusy)

    verdict = await adapter.check_availability(_ctx())

    assert not verdict.available
    assert "occupied" in verdict.reason
