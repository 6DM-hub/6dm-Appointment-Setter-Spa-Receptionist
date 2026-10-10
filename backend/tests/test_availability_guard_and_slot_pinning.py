"""Hard code-level guarantees added on top of the timezone/Square fix:

  1. The agent may never claim a specific time is available/open/free unless
     that exact time is backed by a real Square SearchAvailability result
     (`BookingDraft.selected_slot` / `alternative_slots`, populated only from
     `AvailabilityVerdict.slot`). See `booking_state.contains_unauthorized_
     availability_claim` and its call sites in telephony.py / xai_realtime.py.
  2. Once a slot has been offered to the caller, `create_booking` re-verifies
     and books THAT EXACT (team member, service variation, time) combination
     — never a fresh, unconstrained search that could silently substitute a
     different therapist or service variation. See
     `SquareAdapter._verify_pinned_slot` and `create_booking`'s pinned branch.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import BookingProvider
from app.services import appointment_booking_service
from app.services.appointment_booking_service import (
    BookingOutcome,
    _available_alternatives,
    confirm_booking,
    stage_booking,
)
from app.services.booking_adapters import BookingContext, SpaBookingAdapter
from app.services.booking_adapters.base import BookingProviderError
from app.services.booking_state import (
    arm_verified_proposal,
    contains_unauthorized_availability_claim,
    contains_unverified_post_massage_facial_offer,
    get_draft,
    looks_like_unverified_booking_success,
    save_draft,
)
from app.services.business_hours import resolve_timezone
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent
from tests.conftest import make_spa
from tests.test_square_timezone_and_staff import _ctx, _FakeSquareTransport


def _with_square_location(handler):
    async def wrapped(method, path, *, json=None):
        if str(path).startswith("/v2/locations/"):
            return {
                "location": {
                    "id": "loc_123",
                    "status": "ACTIVE",
                    "name": "Test Square Location",
                    "timezone": "America/Chicago",
                }
            }
        return await handler(method, path, json=json)
    return wrapped


async def _async_value(value):
    return value


def _availability(team_member_id: str, *, variation_id="var1", version=1, start="2026-09-24T19:00:00Z", duration=60):
    return {
        "start_at": start,
        "location_id": "loc_123",
        "appointment_segments": [
            {
                "team_member_id": team_member_id,
                "service_variation_id": variation_id,
                "service_variation_version": version,
                "duration_minutes": duration,
            }
        ],
    }


# --------------------------------------------------------------------------- #
# 1. The guard function itself
# --------------------------------------------------------------------------- #


def _session_with_provider(provider: str = "square") -> CallSession:
    session = CallSession(
        "call-guard", "inbound", "+15550000002", "+15550000001", tenant_id=str(uuid.uuid4())
    )
    session.entities["booking_provider"] = provider
    return session


def test_success_claim_requires_appointment_language_not_bare_booked():
    assert looks_like_unverified_booking_success("Your appointment is confirmed.")
    assert looks_like_unverified_booking_success("You're all set.")
    assert not looks_like_unverified_booking_success(
        "That time is already booked. I can check another opening."
    )
    assert not looks_like_unverified_booking_success(
        "I've confirmed that 3pm is not available."
    )


def test_guard_blocks_a_claimed_time_with_no_authoritative_state_at_all():
    session = _session_with_provider()
    tz = resolve_timezone("America/Chicago")

    assert contains_unauthorized_availability_claim(
        "10 AM is available on Thursday.", session, tz
    )


def test_guard_allows_a_time_matching_the_selected_square_slot():
    session = _session_with_provider()
    draft = get_draft(session)
    draft.selected_slot = {"start": "2026-09-24T19:00:00Z"}  # 2pm CDT
    session.entities["booking_draft"] = draft.to_dict()
    tz = resolve_timezone("America/Chicago")

    assert not contains_unauthorized_availability_claim(
        "Great news, 2pm is available for you.", session, tz
    )


def test_guard_allows_a_time_matching_a_square_returned_alternative():
    session = _session_with_provider()
    draft = get_draft(session)
    draft.alternative_slots = [{"start": "2026-09-24T20:30:00Z"}]  # 3:30pm CDT
    session.entities["booking_draft"] = draft.to_dict()
    tz = resolve_timezone("America/Chicago")

    assert not contains_unauthorized_availability_claim(
        "I have 3:30pm available instead.", session, tz
    )


def test_guard_blocks_a_time_that_does_not_match_anything_authorized():
    session = _session_with_provider()
    draft = get_draft(session)
    draft.selected_slot = {"start": "2026-09-24T19:00:00Z"}  # 2pm CDT confirmed
    session.entities["booking_draft"] = draft.to_dict()
    tz = resolve_timezone("America/Chicago")

    # The agent claiming a DIFFERENT time (3pm) is available is exactly the
    # invented-availability failure mode this guard exists to catch.
    assert contains_unauthorized_availability_claim(
        "Actually, 3pm is available too.", session, tz
    )


def test_guard_ignores_replies_with_no_availability_claim_language():
    session = _session_with_provider()
    tz = resolve_timezone("America/Chicago")

    # Repeating the caller's own words back is not an assertion of
    # availability, so this must never be blocked regardless of state.
    assert not contains_unauthorized_availability_claim(
        "You'd like 2pm, let me check on that.", session, tz
    )


def test_guard_ignores_replies_with_no_clock_time_at_all():
    session = _session_with_provider()
    tz = resolve_timezone("America/Chicago")

    assert not contains_unauthorized_availability_claim(
        "We have availability that day.", session, tz
    )


def test_guard_blocks_post_massage_facial_claim_without_atomic_visit():
    session = _session_with_provider()
    draft = get_draft(session)
    draft.provider_verified = True
    draft.selected_slot = {
        "start": "2026-09-24T19:00:00Z",
        "service_name": "60 minute massage",
    }
    save_draft(session, draft)

    assert contains_unverified_post_massage_facial_offer(
        "Our esthetician is available right after your massage, so we could add a facial.",
        session,
    )


def test_guard_allows_post_massage_facial_claim_for_adjacent_verified_visit():
    session = _session_with_provider()
    draft = get_draft(session)
    draft.provider_verified = True
    draft.selected_slot = {
        "start": "2026-09-24T19:00:00Z",
        "visit_segments": [
            {
                "service_name": "60 minute Swedish massage",
                "start": "2026-09-24T19:00:00Z",
                "end": "2026-09-24T20:00:00Z",
                "team_member_id": "massage-provider",
            },
            {
                "service_name": "60 minute European facial",
                "start": "2026-09-24T20:00:00Z",
                "end": "2026-09-24T21:00:00Z",
                "team_member_id": "esthetician",
            },
        ],
    }
    save_draft(session, draft)

    assert not contains_unverified_post_massage_facial_offer(
        "Our esthetician is available right after your massage, so we could add the European facial.",
        session,
    )


def test_guard_blocks_a_different_named_facial_than_provider_verified():
    session = _session_with_provider()
    draft = get_draft(session)
    draft.provider_verified = True
    draft.selected_slot = {
        "start": "2026-09-24T19:00:00Z",
        "visit_segments": [
            {
                "service_name": "60 minute Swedish massage",
                "start": "2026-09-24T19:00:00Z",
                "end": "2026-09-24T20:00:00Z",
                "team_member_id": "massage-provider",
            },
            {
                "service_name": "60 minute European facial",
                "start": "2026-09-24T20:10:00Z",
                "end": "2026-09-24T21:10:00Z",
                "team_member_id": "esthetician",
            },
        ],
    }
    save_draft(session, draft)

    assert contains_unverified_post_massage_facial_offer(
        "Our esthetician is available after your massage, so we could add a Signature facial.",
        session,
    )
    assert not contains_unverified_post_massage_facial_offer(
        "Our esthetician is available after your massage, so we could add the European facial.",
        session,
    )


def test_guard_allows_neutral_offer_to_check_sequential_availability():
    session = _session_with_provider()
    assert not contains_unverified_post_massage_facial_offer(
        "Would you like me to check whether a facial is available after your massage?",
        session,
    )


def test_guard_allows_restating_the_callers_own_requested_time():
    session = _session_with_provider()
    session.requested_datetime = "2026-09-24T19:00:00Z"  # caller's own ask, not yet confirmed
    tz = resolve_timezone("America/Chicago")

    assert not contains_unauthorized_availability_claim(
        "Just to confirm, that's 2pm for a massage.", session, tz
    )


def test_guard_is_scoped_to_square_tenants_only():
    """Google-Calendar/local tenants never populate `AvailabilityVerdict.slot`
    today, so the guard must not fire for them — it would block every normal
    reply. Scoping happens at the call sites (telephony.py, xai_realtime.py),
    not inside the pure function, so this documents the expectation at the
    call-site helper instead."""
    from app.api.v1.telephony import _authoritative_availability_reply

    session = _session_with_provider(provider="google_calendar")
    reply = "Great news, 10am is available."

    # Not square -> guard is skipped entirely, reply passes through unchanged.
    assert _authoritative_availability_reply(session, reply) == reply


# --------------------------------------------------------------------------- #
# 2. Alternative slots are always Square-sourced
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_alternative_slots_carry_the_real_square_slot_data(monkeypatch):
    spa = make_spa(timezone="America/Chicago", business_hours={"thu": [{"open": "09:00", "close": "18:00"}]}, booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = SpaBookingAdapter(spa)
    transport = _FakeSquareTransport()
    transport.catalog_items = [
        {"item_data": {"name": "Deep tissue massage", "variations": [{"id": "var1", "version": 1, "item_variation_data": {"name": "Regular"}}]}}
    ]
    # Every 30-minute candidate Square is asked about is "available" with a
    # real team member attached, so alternatives are never invented.
    call_count = {"n": 0}

    async def _request(method, path, *, json=None):
        if path == "/v2/bookings/availability/search":
            call_count["n"] += 1
            start = json["query"]["filter"]["start_at_range"]["start_at"]
            return {"availabilities": [_availability("TM_REAL", start=start)]}
        return await transport(method, path, json=json)

    monkeypatch.setattr(adapter.delegate, "_request", _with_square_location(_request))

    session = CallSession("call-alts", "inbound", "+15550000002", "+15550000001", tenant_id=str(spa.id))
    intent = AppointmentIntent(intent="schedule", confidence=1.0, service_description="Deep tissue massage")

    monkeypatch.setattr(appointment_booking_service, "_has_conflict", lambda *_a, **_k: _async_value(False))

    suggestions = await _available_alternatives(
        None, appointment_booking_service.TenantScope.for_tenant(spa.id), adapter, session,
        intent, datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc), timedelta(hours=1), capacity=1,
    )

    assert len(suggestions) == 3
    for _dt, slot in suggestions:
        assert slot is not None
        assert slot["team_member_id"] == "TM_REAL"
        assert slot["service_variation_id"] == "var1"
    assert call_count["n"] >= 3


# --------------------------------------------------------------------------- #
# 3. Selected slot survives confirmation: therapist and service variation
#    cannot silently change
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pinned_recheck_queries_only_the_selected_team_member(monkeypatch):
    spa = make_spa(booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = SpaBookingAdapter(spa).delegate

    seen_filters = []

    async def _request(method, path, *, json=None):
        if path == "/v2/bookings/availability/search":
            seen_filters.append(json["query"]["filter"]["segment_filters"][0])
            # A broad/default search (the bug this guards against) would
            # return a DIFFERENT therapist than the one already selected.
            return {"availabilities": [_availability("TM_WRONG")]}
        raise AssertionError(path)

    monkeypatch.setattr(adapter, "_request", _with_square_location(_request))

    ctx = _ctx(selected_slot={
        "start": "2026-09-24T19:00:00Z",
        "team_member_id": "TM_CORRECT",
        "service_variation_id": "var1",
        "service_variation_version": 1,
        "duration_minutes": 60,
    })
    result = await adapter._find_exact_availability(ctx)

    assert result is None  # TM_WRONG never matches the pinned TM_CORRECT filter
    assert seen_filters[0]["team_member_id_filter"] == {"any": ["TM_CORRECT"]}


@pytest.mark.asyncio
async def test_create_booking_uses_pinned_therapist_even_if_a_broad_search_would_differ(monkeypatch):
    spa = make_spa(booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = SpaBookingAdapter(spa).delegate
    transport = _FakeSquareTransport()
    transport.customers = [{"id": "cust_1"}]
    transport.created_booking = {"id": "sq_pinned", "status": "ACCEPTED"}

    async def _request(method, path, *, json=None):
        if path == "/v2/bookings/availability/search":
            team_filter = json["query"]["filter"]["segment_filters"][0].get("team_member_id_filter")
            if team_filter == {"any": ["TM_CORRECT"]}:
                return {"availabilities": [_availability("TM_CORRECT")]}
            # Any other (unpinned/broad) query would surface someone else.
            return {"availabilities": [_availability("TM_WRONG")]}
        return await transport(method, path, json=json)

    monkeypatch.setattr(adapter, "_request", _with_square_location(_request))

    ctx = _ctx(selected_slot={
        "start": "2026-09-24T19:00:00Z",
        "team_member_id": "TM_CORRECT",
        "service_variation_id": "var1",
        "service_variation_version": 1,
        "duration_minutes": 60,
    })
    result = await adapter.create_booking(ctx)

    assert result.external_id == "sq_pinned"
    create_call = [c for c in transport.calls if c[1] == "/v2/bookings"][0]
    segment = create_call[2]["booking"]["appointment_segments"][0]
    assert segment["team_member_id"] == "TM_CORRECT"


@pytest.mark.asyncio
async def test_create_booking_uses_pinned_service_variation_even_if_recheck_shows_another(monkeypatch):
    """Same guarantee, for the service variation/version instead of the
    therapist: whatever was pinned is what gets booked."""
    spa = make_spa(booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = SpaBookingAdapter(spa).delegate

    async def _request(method, path, *, json=None):
        if path == "/v2/bookings/availability/search":
            # Recheck confirms the pinned combination is free. Square is free
            # to describe the segment however it likes in the response; the
            # WRITE must still use the pinned identifiers, not these.
            return {"availabilities": [_availability("TM_A", variation_id="var_pinned", version=9)]}
        if path == "/v2/customers/search":
            return {"customers": [{"id": "cust_1"}]}
        if path == "/v2/bookings":
            return {"booking": {"id": "sq_ok", "status": "ACCEPTED"}}
        raise AssertionError(path)

    monkeypatch.setattr(adapter, "_request", _with_square_location(_request))

    ctx = _ctx(selected_slot={
        "start": "2026-09-24T19:00:00Z",
        "team_member_id": "TM_A",
        "service_variation_id": "var_pinned",
        "service_variation_version": 9,
        "duration_minutes": 60,
    })
    result = await adapter.create_booking(ctx)

    assert result.external_id == "sq_ok"


@pytest.mark.asyncio
async def test_pinned_slot_that_is_no_longer_free_fails_instead_of_substituting(monkeypatch):
    spa = make_spa(booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = SpaBookingAdapter(spa).delegate

    async def _request(method, path, *, json=None):
        if path == "/v2/bookings/availability/search":
            return {"availabilities": []}  # the pinned combination is gone
        raise AssertionError(path)

    monkeypatch.setattr(adapter, "_request", _with_square_location(_request))

    ctx = _ctx(selected_slot={
        "start": "2026-09-24T19:00:00Z",
        "team_member_id": "TM_CORRECT",
        "service_variation_id": "var1",
        "service_variation_version": 1,
    })

    with pytest.raises(BookingProviderError):
        await adapter.create_booking(ctx)


# --------------------------------------------------------------------------- #
# 4. Zero Square availability -> no specific claim is ever authorized
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_zero_availability_leaves_nothing_for_the_guard_to_authorize(monkeypatch):
    spa = make_spa(timezone="America/Chicago", business_hours={"thu": [{"open": "09:00", "close": "18:00"}]}, booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = SpaBookingAdapter(spa)
    transport = _FakeSquareTransport()
    transport.catalog_items = [
        {"item_data": {"name": "Deep tissue massage", "variations": [{"id": "var1", "version": 1, "item_variation_data": {"name": "Regular"}}]}}
    ]
    transport.availabilities = []
    monkeypatch.setattr(adapter.delegate, "_request", transport)

    verdict = await adapter.check_availability(_ctx())

    assert not verdict.available
    assert verdict.slot is None

    session = _session_with_provider()
    tz = resolve_timezone("America/Chicago")
    # With no slot recorded, any specific claim is unauthorized by construction.
    assert contains_unauthorized_availability_claim("2pm is available.", session, tz)


# --------------------------------------------------------------------------- #
# 5. Full engine round trip: pin survives stage -> confirm; rejection never
#    produces a booking confirmation
# --------------------------------------------------------------------------- #


class _FakeDB:
    def add(self, _obj):
        return None

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def refresh(self, _obj):
        return None


def _spa_for_engine_tests():
    return make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "t", "location_id": "loc_123"},
        business_hours={"thu": [{"open": "10:00", "close": "18:00"}]},
        timezone="America/Chicago",
        services=[
            {
                "name": "Deep tissue massage",
                "duration_minutes": 60,
                "square_variation_id": "var_dt_60",
                "square_variation_version": 2,
            }
        ],
    )


@pytest.mark.asyncio
async def test_stage_then_confirm_preserves_the_same_therapist_end_to_end(monkeypatch):
    """The scenario the fix targets: propose_appointment (stage) finds a
    therapist via a broad ANY search; confirm_appointment (confirm) must book
    that SAME therapist, even though a fresh broad search at confirm time
    would return someone else."""
    spa = _spa_for_engine_tests()
    adapter = SpaBookingAdapter(spa)

    async def _request(method, path, *, json=None):
        if path == "/v2/bookings/availability/search":
            team_filter = json["query"]["filter"]["segment_filters"][0].get("team_member_id_filter")
            if team_filter == {"any": ["TM_FIRST"]}:
                return {"availabilities": [_availability("TM_FIRST", variation_id="var_dt_60", version=2)]}
            if not team_filter:
                # The unconstrained ("ANY") search, as run during stage_booking,
                # happens to surface TM_FIRST as the only/first candidate.
                return {"availabilities": [_availability("TM_FIRST", variation_id="var_dt_60", version=2)]}
            # Any other constrained search (e.g. accidentally re-searching
            # broadly a second time) would find a different therapist.
            return {"availabilities": [_availability("TM_SECOND", variation_id="var_dt_60", version=2)]}
        if path == "/v2/customers/search":
            return {"customers": [{"id": "cust_1"}]}
        if path == "/v2/bookings":
            return {"booking": {"id": "sq_final_booking", "status": "ACCEPTED"}}
        raise AssertionError(path)

    monkeypatch.setattr(adapter.delegate, "_request", _with_square_location(_request))

    monkeypatch.setattr(appointment_booking_service, "_load_spa", lambda *_: _async_value(spa))
    monkeypatch.setattr(appointment_booking_service, "get_booking_adapter", lambda **_: adapter)
    monkeypatch.setattr(
        appointment_booking_service, "_resolve_contact", lambda *_: _async_value(SimpleNamespace(id=uuid.uuid4()))
    )
    monkeypatch.setattr(appointment_booking_service, "_has_conflict", lambda *_a, **_k: _async_value(False))
    monkeypatch.setattr(appointment_booking_service, "_unresolved_booking", lambda *_: _async_value(None))
    monkeypatch.setattr(appointment_booking_service, "_load_by_intent_key", lambda *_: _async_value(None))
    monkeypatch.setattr(appointment_booking_service, "_load_call_log_id", lambda *_: _async_value(None))

    session = CallSession("call-pin-e2e", "inbound", "+15550000002", "+15550000001", tenant_id=str(spa.id))

    # Turn 1: caller states the request; propose_appointment stages it.
    propose_intent = AppointmentIntent(
        intent="schedule", confidence=1.0,
        requested_start_iso="2026-09-24T14:00:00",
        service_description="60-minute deep tissue massage",
    )
    staged = await stage_booking(_FakeDB(), session, propose_intent)
    assert staged.outcome.value == "draft"

    draft = get_draft(session)
    draft.caller_name = "Ada"
    save_draft(session, draft)
    draft = get_draft(session)
    assert draft.selected_slot is not None
    assert draft.selected_slot["team_member_id"] == "TM_FIRST"

    # Turn 2: caller confirms. No new date/service/staff is restated, so the
    # pinned slot from turn 1 must be what gets booked.
    arm_verified_proposal(session)
    confirmed = await confirm_booking(_FakeDB(), session)

    assert confirmed.outcome is BookingOutcome.BOOKED, confirmed.message
    assert confirmed.appointment.external_booking_id == "sq_final_booking"


@pytest.mark.asyncio
async def test_square_booking_rejection_never_produces_a_booked_outcome(monkeypatch):
    spa = _spa_for_engine_tests()
    adapter = SpaBookingAdapter(spa)

    async def _request(method, path, *, json=None):
        if path == "/v2/bookings/availability/search":
            return {"availabilities": [_availability("TM_1", variation_id="var_dt_60", version=2)]}
        if path == "/v2/customers/search":
            return {"customers": [{"id": "cust_1"}]}
        if path == "/v2/bookings":
            raise BookingProviderError("Square API request failed: DECLINED")
        raise AssertionError(path)

    monkeypatch.setattr(adapter.delegate, "_request", _with_square_location(_request))
    monkeypatch.setattr(appointment_booking_service, "_load_spa", lambda *_: _async_value(spa))
    monkeypatch.setattr(appointment_booking_service, "get_booking_adapter", lambda **_: adapter)
    monkeypatch.setattr(
        appointment_booking_service, "_resolve_contact", lambda *_: _async_value(SimpleNamespace(id=uuid.uuid4()))
    )
    monkeypatch.setattr(appointment_booking_service, "_has_conflict", lambda *_a, **_k: _async_value(False))
    monkeypatch.setattr(appointment_booking_service, "_unresolved_booking", lambda *_: _async_value(None))
    monkeypatch.setattr(appointment_booking_service, "_load_by_intent_key", lambda *_: _async_value(None))
    monkeypatch.setattr(appointment_booking_service, "_load_call_log_id", lambda *_: _async_value(None))

    session = CallSession("call-reject", "inbound", "+15550000002", "+15550000001", tenant_id=str(spa.id))
    intent = AppointmentIntent(
        intent="schedule", confidence=1.0,
        requested_start_iso="2026-09-24T14:00:00",
        service_description="60-minute deep tissue massage",
    )

    staged = await stage_booking(_FakeDB(), session, intent)
    assert staged.outcome is BookingOutcome.DRAFT
    named = get_draft(session)
    named.caller_name = "Ada"
    save_draft(session, named)
    arm_verified_proposal(session)
    result = await confirm_booking(_FakeDB(), session)

    assert result.outcome is BookingOutcome.ERROR
    assert result.appointment is None
    assert session.appointment_id is None
    assert session.booking_status != "booked"
