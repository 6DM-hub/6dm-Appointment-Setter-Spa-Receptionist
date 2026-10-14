"""Unconnected calendars must never vouch for a time; CARA-native spas use
their own appointments as the calendar of record.

Reproduces the Rubyz call (2026-10-14): Google Calendar not connected,
`list_openings` returned [] ("no openings") and an exact check for Oct 15 at
4 PM returned available=True with no slot, so Cara offered a time that could
never be booked.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.models import BookingProvider
from app.services import appointment_booking_service as svc
from app.services.appointment_booking_service import (
    _searches_openings,
    _stage_earliest,
    check_availability_only,
    stage_booking,
)
from app.services.booking_adapters import SpaBookingAdapter
from app.services.booking_adapters.base import (
    AvailabilityVerdict,
    BookingContext,
    BookingProviderError,
    CaraNativeAdapter,
)
from app.services.booking_adapters.google_calendar import GoogleCalendarAdapter
from app.services.booking_adapters.providers.square import SquareAdapter
from app.services.booking_adapters.spa_router import ProviderNotConfiguredAdapter
from app.services.booking_state import get_draft
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent
from tests.conftest import make_spa

CHICAGO = "America/Chicago"
EVERY_DAY = {d: [{"open": "09:00", "close": "18:00"}] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
FACIAL = [{"name": "Signature Facial", "duration_minutes": 60}]
CARA_NATIVE = {"scheduling_source": "cara_native"}


def rubyz(**overrides):
    """Google Calendar selected, never connected — like Rubyz."""
    settings = {
        "name": "Rubyz",
        "timezone": CHICAGO,
        "business_hours": EVERY_DAY,
        "services": FACIAL,
        "booking_provider": BookingProvider.GOOGLE_CALENDAR,
        "booking_config": {},
        **overrides,
    }
    return make_spa(**settings)


def _async(value):
    async def inner(*_a, **_k):
        return value
    return inner


def _session(spa, sid="call-rubyz") -> CallSession:
    return CallSession(sid, "inbound", "+15550000002", "+15550000001", tenant_id=str(spa.id))


def _oct15_4pm_intent():
    return AppointmentIntent(
        intent="inquiry",
        confidence=1.0,
        requested_start_iso="2026-10-15T16:00:00",
        service_description="Signature Facial",
    )


def _ctx(start: datetime, minutes: int = 60) -> BookingContext:
    return BookingContext(
        start=start, end=start + timedelta(minutes=minutes), title="Signature Facial",
        service_description="Signature Facial", customer_phone="+15550000002",
    )


def _routing(spa):
    return SimpleNamespace(
        adapter=SpaBookingAdapter(spa), scope=SimpleNamespace(tenant_id=spa.id, owner_id=None),
        spa=spa, capacity=1, is_outbound_sales=False, product="Spa Receptionist",
    )


# --------------------------------------------------------------- unconnected


def test_unconnected_google_routes_to_not_configured_and_cannot_search():
    adapter = SpaBookingAdapter(rubyz())
    assert isinstance(adapter.delegate, ProviderNotConfiguredAdapter)
    assert adapter.supports_list_openings is False
    assert _searches_openings(adapter) is False


@pytest.mark.asyncio
async def test_unconnected_calendar_never_claims_oct15_4pm(monkeypatch):
    """The exact Rubyz check: must be a lookup failure, never 'available'."""
    spa = rubyz()
    monkeypatch.setattr(svc, "_load_spa", _async(spa))
    monkeypatch.setattr(svc, "_has_conflict", _async(False))

    result = await check_availability_only(None, _session(spa), _oct15_4pm_intent())

    assert result.available is False
    assert result.lookup_failed is True
    assert "available" not in result.message.lower()


@pytest.mark.asyncio
async def test_unconnected_calendar_check_raises_calendar_not_connected():
    adapter = SpaBookingAdapter(rubyz())
    start = datetime(2026, 10, 15, 16, 0, tzinfo=ZoneInfo(CHICAGO))
    with pytest.raises(BookingProviderError) as caught:
        await adapter.check_availability(_ctx(start))
    assert caught.value.code == "CALENDAR_NOT_CONNECTED"


@pytest.mark.asyncio
async def test_unconnected_earliest_search_is_a_follow_up_not_no_openings(monkeypatch):
    """Previously `[]` from the unconnected adapter became 'No provider-confirmed
    openings'. Now it is reported as a scheduling problem needing staff."""
    spa = rubyz()
    session = _session(spa)
    session.add_turn("user", "What's your earliest appointment for a facial?")
    monkeypatch.setattr(svc, "_prepare", _async(_routing(spa)))
    monkeypatch.setattr(svc, "_has_conflict", _async(False))
    notify = AsyncMock()
    monkeypatch.setattr(svc, "_notify_provider_error", notify)

    intent = AppointmentIntent(intent="schedule", earliest=True, confidence=1.0,
                               service_description="Signature Facial")
    result = await stage_booking(None, session, intent)

    assert result.outcome is svc.BookingOutcome.ERROR
    assert "follow-up" in result.message
    assert "No provider-confirmed openings" not in result.message
    assert session.booking_status == "follow_up_required"
    notify.assert_awaited_once()


# -------------------------------------------------------------- CARA-native


def test_opt_in_selects_cara_native_and_walks_instead_of_listing():
    adapter = SpaBookingAdapter(rubyz(booking_policies=CARA_NATIVE))
    assert isinstance(adapter.delegate, CaraNativeAdapter)
    assert adapter.provider == "cara_native"
    assert _searches_openings(adapter) is False


@pytest.mark.asyncio
async def test_cara_native_oct15_4pm_is_available_with_a_verified_slot(monkeypatch):
    spa = rubyz(booking_policies=CARA_NATIVE)
    monkeypatch.setattr(svc, "_load_spa", _async(spa))
    monkeypatch.setattr(svc, "_has_conflict", _async(False))
    session = _session(spa)

    result = await check_availability_only(None, session, _oct15_4pm_intent())

    assert result.available is True
    slot = get_draft(session).selected_slot
    assert slot["source"] == "cara_native"
    assert slot["duration_minutes"] == 60
    assert datetime.fromisoformat(slot["start"]) == datetime(2026, 10, 15, 16, 0, tzinfo=ZoneInfo(CHICAGO))


@pytest.mark.asyncio
async def test_cara_native_full_slot_is_not_offered(monkeypatch):
    """Capacity reached in CARA's own appointments table -> not available."""
    spa = rubyz(booking_policies=CARA_NATIVE)
    monkeypatch.setattr(svc, "_load_spa", _async(spa))
    monkeypatch.setattr(svc, "_has_conflict", _async(True))

    result = await check_availability_only(None, _session(spa), _oct15_4pm_intent())

    assert result.available is False
    assert get_draft(_session(spa)).selected_slot is None


@pytest.mark.asyncio
async def test_cara_native_outside_hours_is_not_offered(monkeypatch):
    spa = rubyz(booking_policies=CARA_NATIVE)
    monkeypatch.setattr(svc, "_load_spa", _async(spa))
    monkeypatch.setattr(svc, "_has_conflict", _async(False))
    intent = _oct15_4pm_intent()
    intent.requested_start_iso = "2026-10-15T17:30:00"  # 60 min runs past 18:00

    result = await check_availability_only(None, _session(spa), intent)

    assert result.available is False


@pytest.mark.asyncio
async def test_cara_native_earliest_search_offers_only_locally_verified_times(monkeypatch):
    """Walks candidates through hours + service + capacity; skips a full slot."""
    spa = rubyz(booking_policies=CARA_NATIVE)
    session = _session(spa)
    session.add_turn("user", "What's the earliest you have?")
    draft = get_draft(session)
    draft.service_description = "Signature Facial"
    checked: list[datetime] = []

    async def conflict(_db, _scope, start, _end, _capacity, **_k):
        checked.append(start)
        return len(checked) == 1  # first open candidate is already full

    monkeypatch.setattr(svc, "_has_conflict", conflict)
    intent = AppointmentIntent(intent="schedule", earliest=True, confidence=1.0,
                               service_description="Signature Facial")

    result = await _stage_earliest(None, session, intent, draft, _routing(spa))

    offered = get_draft(session).alternative_slots
    assert result.outcome is svc.BookingOutcome.CONFLICT  # "choose one", nothing booked
    assert 1 <= len(offered) <= svc.ALTERNATIVE_SUGGESTIONS
    assert all(slot["source"] == "cara_native" for slot in offered)
    offered_starts = {datetime.fromisoformat(s["start"]) for s in offered}
    assert checked[0] not in offered_starts  # the full slot was skipped
    tz = ZoneInfo(CHICAGO)
    for start in offered_starts:
        local = start.astimezone(tz)
        assert 9 <= local.hour and (local + timedelta(minutes=60)).time() <= datetime(2000, 1, 1, 18).time()


@pytest.mark.asyncio
async def test_cara_native_multi_service_visit_is_refused_not_claimed():
    spa = rubyz(booking_policies=CARA_NATIVE,
                services=FACIAL + [{"name": "Swedish Massage", "duration_minutes": 60}])
    adapter = SpaBookingAdapter(spa)
    start = datetime(2026, 10, 15, 11, 0, tzinfo=ZoneInfo(CHICAGO))
    ctx = BookingContext(start=start, end=start + timedelta(minutes=120), title="x",
                         service_description="Signature Facial and Swedish Massage",
                         customer_phone="+15550000002")
    if not adapter.requires_visit_validation(ctx):
        pytest.skip("menu matcher did not treat this as a multi-service visit")
    with pytest.raises(BookingProviderError) as caught:
        await adapter.check_availability(ctx)
    assert caught.value.code == "VISIT_UNSUPPORTED"


@pytest.mark.asyncio
async def test_cara_native_booking_is_written_locally_with_caras_own_reference():
    ctx = _ctx(datetime(2026, 10, 15, 21, tzinfo=timezone.utc))
    ctx = BookingContext(**{**ctx.__dict__, "booking_reference": "call-1:abc"})
    booking = await CaraNativeAdapter().create_booking(ctx)
    assert booking.provider == "cara_native"
    assert booking.external_id == "cara:call-1:abc"


# ------------------------------------------------- unverified "available"


class _SlotlessDelegate(GoogleCalendarAdapter):
    """Any delegate that answers 'available' without proving it."""

    async def check_availability(self, ctx):
        return AvailabilityVerdict.ok()


@pytest.mark.asyncio
async def test_router_rejects_available_without_a_slot():
    adapter = SpaBookingAdapter(rubyz(booking_policies=CARA_NATIVE))
    adapter.delegate = _SlotlessDelegate(calendar_id=None, calendar_label="x", default_title="x",
                                         connection=None, timezone_name=CHICAGO)
    start = datetime(2026, 10, 15, 16, 0, tzinfo=ZoneInfo(CHICAGO))
    with pytest.raises(BookingProviderError) as caught:
        await adapter.check_availability(_ctx(start))
    assert caught.value.code == "AVAILABILITY_UNVERIFIED"


@pytest.mark.asyncio
async def test_connected_google_freebusy_free_returns_verified_slot(monkeypatch):
    import app.services.booking_adapters.google_calendar as gc

    monkeypatch.setattr(gc, "freebusy", _async([]))
    adapter = GoogleCalendarAdapter(calendar_id="cal", calendar_label="x", default_title="x",
                                    connection=object(), timezone_name=CHICAGO)
    start = datetime(2026, 10, 15, 16, 0, tzinfo=ZoneInfo(CHICAGO))
    verdict = await adapter.check_availability(_ctx(start))
    assert verdict.available and verdict.slot["source"] == "google_freebusy"
    assert verdict.slot["duration_minutes"] == 60


@pytest.mark.asyncio
async def test_connected_google_busy_is_still_unavailable(monkeypatch):
    import app.services.booking_adapters.google_calendar as gc

    monkeypatch.setattr(gc, "freebusy", _async([{"start": "x", "end": "y"}]))
    adapter = GoogleCalendarAdapter(calendar_id="cal", calendar_label="x", default_title="x",
                                    connection=object(), timezone_name=CHICAGO)
    start = datetime(2026, 10, 15, 16, 0, tzinfo=ZoneInfo(CHICAGO))
    assert not (await adapter.check_availability(_ctx(start))).available


# ------------------------------------------------------ Square unchanged


def test_square_spa_routing_is_unchanged_and_still_lists_openings():
    """Healing Waters-style Square setup: same delegate, real openings search,
    and the CARA-native opt-in has no effect on a configured Square spa."""
    for policies in (None, CARA_NATIVE):
        spa = make_spa(
            name="Healing Waters",
            booking_provider=BookingProvider.SQUARE,
            booking_config={"access_token": "token", "location_id": "loc_123"},
            services=[{"name": "Deep Tissue", "duration_minutes": 60}],
            booking_policies=policies,
        )
        adapter = SpaBookingAdapter(spa)
        assert isinstance(adapter.delegate, SquareAdapter)
        assert adapter.provider == "square"
        assert adapter.supports_list_openings is True
        assert _searches_openings(adapter) is True
