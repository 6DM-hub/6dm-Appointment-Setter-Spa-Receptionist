"""Regression coverage for "the second one" (Phase 1, offered-slot selection).

The bug: when the caller picks one of the alternatives just offered (e.g. "the
second one"), the model re-states it as a plain start time and that re-enters
`stage_booking` exactly like a brand-new request. Before this fix,
`stage()` (booking_state.py) unconditionally wiped `selected_slot` and
`alternative_slots` the moment the start time changed, so the caller's pick
triggered a fresh, UNCONSTRAINED Square availability search for that clock
time — which can legitimately return a *different* therapist than the one
originally offered if more than one staff member is free at that instant.

The fix: `stage_booking` (appointment_booking_service.py) now matches the new
start against the alternatives that were on the draft just before `stage()`
cleared them (`_match_alternative_slot`), and when it matches, pins that exact
stored slot onto `BookingContext.selected_slot` so the availability recheck
goes through Square's `_verify_pinned_slot` path (exact team member + service
variation) instead of a broad search.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import BookingProvider
from app.services import appointment_booking_service
from app.services.appointment_booking_service import BookingOutcome, stage_booking
from app.services.booking_adapters import SpaBookingAdapter
from app.services.booking_state import save_draft
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent
from tests.conftest import make_spa


async def _async_value(value):
    return value


class _FakeTransport:
    """Square transport that returns a DIFFERENT therapist depending on
    whether the availability search is filtered to one specific team member
    (the pinned recheck) or left open (a broad, unconstrained search)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, method: str, path: str, *, json: dict | None = None) -> dict:
        self.calls.append((path, json or {}))
        if str(path).startswith("/v2/locations/"):
            return {
                "location": {
                    "id": "loc_123",
                    "name": "Test Square Location",
                    "timezone": "UTC",
                    "status": "ACTIVE",
                }
            }
        if path == "/v2/bookings/availability/search":
            requested_start = (
                (json or {}).get("query", {}).get("filter", {})
                .get("start_at_range", {}).get("start_at")
            )
            segment_filter = (
                (json or {}).get("query", {}).get("filter", {}).get("segment_filters", [{}])[0]
            )
            team_filter = segment_filter.get("team_member_id_filter")
            if team_filter and team_filter.get("any"):
                team_member_id = team_filter["any"][0]
            else:
                # Broad/unconstrained search: a DIFFERENT therapist is free at
                # the exact same clock time. Without the fix this is what
                # "the second one" would silently resolve to.
                team_member_id = "TM_FIRST"
            return {
                "availabilities": [
                    {
                        "start_at": requested_start,
                        "location_id": "loc_123",
                        "appointment_segments": [
                            {
                                "team_member_id": team_member_id,
                                "service_variation_id": "var1",
                                "service_variation_version": 4,
                                "duration_minutes": 60,
                            }
                        ],
                    }
                ]
            }
        raise AssertionError(f"unexpected Square path called: {path}")

    def calls_to(self, path: str) -> list[dict]:
        return [payload for p, payload in self.calls if p == path]


def _spa():
    return make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "t", "location_id": "loc_123"},
        business_hours={"thu": [{"open": "10:00", "close": "18:00"}]},
        timezone="UTC",
        services=[
            {
                "name": "Deep tissue massage",
                "duration_minutes": 60,
                "square_variation_id": "var1",
                "square_variation_version": 4,
            }
        ],
    )


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


def _wire(monkeypatch, spa, adapter):
    monkeypatch.setattr(appointment_booking_service, "_load_spa", lambda *_: _async_value(spa))
    monkeypatch.setattr(appointment_booking_service, "get_booking_adapter", lambda **_: adapter)
    monkeypatch.setattr(
        appointment_booking_service, "_resolve_contact", lambda *_a, **_k: _async_value(SimpleNamespace(id=uuid.uuid4()))
    )
    monkeypatch.setattr(appointment_booking_service, "_has_conflict", lambda *_a, **_k: _async_value(False))
    monkeypatch.setattr(appointment_booking_service, "_load_by_intent_key", lambda *_: _async_value(None))
    monkeypatch.setattr(appointment_booking_service, "_load_call_log_id", lambda *_: _async_value(None))
    monkeypatch.setattr(appointment_booking_service, "persist_caller_identity", lambda *_a, **_k: _async_value(None))


@pytest.mark.asyncio
async def test_selecting_a_previously_offered_alternative_pins_its_exact_therapist(monkeypatch):
    spa = _spa()
    adapter = SpaBookingAdapter(spa)
    transport = _FakeTransport()
    monkeypatch.setattr(adapter.delegate, "_request", transport)
    _wire(monkeypatch, spa, adapter)

    session = CallSession("call-alt-1", "inbound", "+15550000002", "+15550000001", tenant_id=str(spa.id))

    # The caller was already offered two alternatives for the same clock time
    # slate; "the second one" is TM_SECOND at 2026-09-24T14:00:00Z.
    first_alt = {
        "start": "2026-09-24T14:00:00Z",
        "location_id": "loc_123",
        "team_member_id": "TM_FIRST",
        "service_variation_id": "var1",
        "service_variation_version": 4,
        "duration_minutes": 60,
        "provider": "square",
    }
    # The realistic "second one": a distinct time, offered with its own
    # authoritative therapist (`_available_alternatives` walks distinct
    # candidate times, never producing two alternatives at the same instant).
    second_alt = dict(
        first_alt, start="2026-09-24T15:00:00Z", team_member_id="TM_SECOND"
    )
    draft = appointment_booking_service.get_draft(session)
    draft.alternative_slots = [first_alt, second_alt]
    save_draft(session, draft)

    # The model restates "the second one" as its plain start time.
    intent = AppointmentIntent(
        intent="schedule",
        confidence=1.0,
        requested_start_iso="2026-09-24T15:00:00Z",
        service_description="Deep tissue massage",
    )

    result = await stage_booking(_FakeDB(), session, intent)

    assert result.outcome is BookingOutcome.DRAFT, result.message
    updated = appointment_booking_service.get_draft(session)
    assert updated.selected_slot["team_member_id"] == "TM_SECOND"

    # The pinned recheck must have been used, not a broad/unconstrained search.
    search_calls = transport.calls_to("/v2/bookings/availability/search")
    assert len(search_calls) == 1
    segment_filter = search_calls[0]["query"]["filter"]["segment_filters"][0]
    assert segment_filter.get("team_member_id_filter") == {"any": ["TM_SECOND"]}


@pytest.mark.asyncio
async def test_request_not_matching_any_offered_alternative_runs_a_fresh_search(monkeypatch):
    """A genuinely new time (not one of the alternatives) must not be pinned
    to a stale slot — it goes through the normal broad search."""
    spa = _spa()
    adapter = SpaBookingAdapter(spa)
    transport = _FakeTransport()
    monkeypatch.setattr(adapter.delegate, "_request", transport)
    _wire(monkeypatch, spa, adapter)

    session = CallSession("call-alt-2", "inbound", "+15550000002", "+15550000001", tenant_id=str(spa.id))
    draft = appointment_booking_service.get_draft(session)
    draft.alternative_slots = [
        {
            "start": "2026-09-24T14:00:00Z",
            "location_id": "loc_123",
            "team_member_id": "TM_SECOND",
            "service_variation_id": "var1",
            "service_variation_version": 4,
            "duration_minutes": 60,
            "provider": "square",
        }
    ]
    save_draft(session, draft)

    intent = AppointmentIntent(
        intent="schedule",
        confidence=1.0,
        # A different time than any alternative on file.
        requested_start_iso="2026-09-24T15:00:00Z",
        service_description="Deep tissue massage",
    )

    result = await stage_booking(_FakeDB(), session, intent)

    assert result.outcome is BookingOutcome.DRAFT, result.message
    search_calls = transport.calls_to("/v2/bookings/availability/search")
    segment_filter = search_calls[0]["query"]["filter"]["segment_filters"][0]
    assert "team_member_id_filter" not in segment_filter
