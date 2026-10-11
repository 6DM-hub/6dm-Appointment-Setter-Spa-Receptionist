"""Phase 1 backend coverage for the "model must never invent a date/time"
rule (see also `tests/test_realtime_response_lifecycle.py` for the
realtime-layer turn-grounding guard).

Covers, against the real `stage_booking` engine:

  A. service known, no date at all -> MISSING_INFO, zero provider calls.
  G/H. "earliest opening" -> one deterministic backend forward search
       (`_stage_earliest`, reusing `_available_alternatives`), returning up
       to three real, provider-checked slots — never a caller/model-supplied
       time.
  I/J. generic "HydroLux5" with several configured variations -> asks for
       clarification, zero Square AVAILABILITY calls (catalog resolution
       fails before ever reaching the availability endpoint); the exact
       variation proceeds normally once explicitly named.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import BookingProvider
from app.services import appointment_booking_service
from app.services.appointment_booking_service import BookingOutcome, stage_booking
from app.services.booking_adapters import SpaBookingAdapter
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent
from tests.conftest import make_spa


async def _async_value(value):
    return value


class _FakeTransport:
    """Routes Square `_request` calls by path; records every call made.

    `availability_by_start` maps a requested UTC start_at string to the
    availabilities Square returns for it — lets the earliest-search tests
    make later, further-out candidates free while nearer ones are busy,
    without needing a real provider.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.catalog_items: list[dict] = []
        self.availability_by_start: dict[str, list[dict]] = {}
        self.customers: list[dict] = []
        self.created_customer: dict = {"id": "cust_1"}
        # Number of the first N availability searches to answer "busy" —
        # used by the earliest-search test, where the exact anchor instant
        # (`datetime.now()` inside the code under test) can't be predicted
        # from the test, so slots are made available by SEARCH ORDER instead
        # of by exact timestamp.
        self.busy_for_first_n_searches = 0
        self.echo_range_start = False
        self._availability_search_count = 0

    async def __call__(self, method: str, path: str, *, json: dict | None = None) -> dict:
        self.calls.append((method, path, json or {}))
        if path.startswith("/v2/locations/"):
            loc_id = path.rsplit("/", 1)[-1]
            return {
                "location": {
                    "id": loc_id,
                    "name": "Test Square Location",
                    "timezone": "UTC",
                    "status": "ACTIVE",
                }
            }
        if path == "/v2/catalog/search-catalog-items":
            return {"items": self.catalog_items}
        if path == "/v2/bookings/availability/search":
            self._availability_search_count += 1
            start_at = (json or {}).get("query", {}).get("filter", {}).get(
                "start_at_range", {}
            ).get("start_at")
            if start_at in self.availability_by_start:
                return {"availabilities": self.availability_by_start[start_at]}
            if self._availability_search_count <= self.busy_for_first_n_searches:
                return {"availabilities": []}
            if self.busy_for_first_n_searches or self.echo_range_start:
                return {
                    "availabilities": [
                        {
                            "start_at": start_at,
                            "location_id": "loc_123",
                            "appointment_segments": [
                                {
                                    "team_member_id": "TM1",
                                    "service_variation_id": "var_dt_60",
                                    "service_variation_version": 1,
                                    "duration_minutes": 60,
                                }
                            ],
                        }
                    ]
                }
            return {"availabilities": []}
        if path == "/v2/customers/search":
            return {"customers": self.customers}
        if path == "/v2/customers":
            return {"customer": self.created_customer}
        raise AssertionError(f"unexpected Square path called: {path}")

    def calls_to(self, path: str) -> list[dict]:
        return [payload for _, p, payload in self.calls if p == path]


_HYDROLUX5_SERVICES = [
    {
        "name": "HydroLux5 Facial - Face Only",
        "duration_minutes": 45,
        "square_variation_id": "var_face_only",
        "square_variation_version": 3,
    },
    {
        "name": "HydroLux5 Facial - Face + Neck",
        "duration_minutes": 60,
        "square_variation_id": "var_face_neck",
        "square_variation_version": 5,
    },
    {
        "name": "HydroLux5 Facial - Face + Neck + Chest",
        "duration_minutes": 75,
        "square_variation_id": "var_face_neck_chest",
        "square_variation_version": 2,
    },
]


def _spa(**overrides):
    defaults = dict(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "t", "location_id": "loc_123"},
        business_hours={
            day: [{"open": "00:00", "close": "23:59"}]
            for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        },
        timezone="UTC",
        services=[
            {
                "name": "Deep tissue massage",
                "duration_minutes": 60,
                "square_variation_id": "var_dt_60",
                "square_variation_version": 1,
            }
        ],
    )
    defaults.update(overrides)
    return make_spa(**defaults)


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
        appointment_booking_service, "_resolve_contact",
        lambda *_a, **_k: _async_value(SimpleNamespace(id=uuid.uuid4())),
    )
    monkeypatch.setattr(appointment_booking_service, "_has_conflict", lambda *_a, **_k: _async_value(False))
    monkeypatch.setattr(appointment_booking_service, "_load_by_intent_key", lambda *_: _async_value(None))
    monkeypatch.setattr(appointment_booking_service, "_load_call_log_id", lambda *_: _async_value(None))
    monkeypatch.setattr(appointment_booking_service, "persist_caller_identity", lambda *_a, **_k: _async_value(None))


def _session(call_sid: str, spa, *, history: list[dict] | None = None) -> CallSession:
    return CallSession(
        call_sid, "inbound", "+15550000002", "+15550000001", tenant_id=str(spa.id),
        history=history or [],
    )


_EARLIEST_TURN = [{"role": "user", "content": "Can I get the earliest available appointment?"}]


# --------------------------------------------------------------------------- #
# A. service known, no date -> zero provider calls
# --------------------------------------------------------------------------- #
async def test_service_only_no_date_asks_for_a_date_without_touching_the_provider(monkeypatch):
    spa = _spa()
    adapter = SpaBookingAdapter(spa)
    transport = _FakeTransport()
    monkeypatch.setattr(adapter.delegate, "_request", transport)
    _wire(monkeypatch, spa, adapter)

    intent = AppointmentIntent(
        intent="schedule", confidence=1.0, service_description="Deep tissue massage",
    )
    result = await stage_booking(_FakeDB(), _session("call-a", spa), intent)

    assert result.outcome is BookingOutcome.MISSING_INFO
    assert transport.calls == [], "no Square call may happen before a date/time exists"


# --------------------------------------------------------------------------- #
# G/H. earliest opening -> one deterministic backend forward search
# --------------------------------------------------------------------------- #
async def test_earliest_request_runs_one_backend_forward_search_with_no_caller_supplied_time(
    monkeypatch,
):
    """Caller asked for the earliest opening with no supplied clock time.

    Square is queried once over a forward range (`list_openings`). Returned
    slots are whatever the fake provider echoed for that range start — never
    a time invented by the model or by a local hours calculation.
    """
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    # Keep the range start away from midnight. Otherwise this test fails for
    # one real clock hour each day when its echoing fake provider returns a
    # 23:00 slot whose 60-minute service crosses the fixture's 23:59 close.
    monkeypatch.setattr(appointment_booking_service, "datetime", _FrozenDateTime)

    spa = _spa()
    adapter = SpaBookingAdapter(spa)
    transport = _FakeTransport()
    transport.echo_range_start = True
    monkeypatch.setattr(adapter.delegate, "_request", transport)
    _wire(monkeypatch, spa, adapter)

    # `earliest=True`; deliberately no requested_start_iso — nothing for the
    # model to have guessed.
    intent = AppointmentIntent(
        intent="schedule", confidence=1.0, service_description="Deep tissue massage",
        earliest=True,
    )
    session = _session("call-earliest", spa, history=list(_EARLIEST_TURN))
    result = await stage_booking(_FakeDB(), session, intent)

    assert result.outcome is BookingOutcome.CONFLICT, result.message
    assert "no longer available" not in result.message.lower()
    draft = appointment_booking_service.get_draft(session)
    assert 1 <= len(draft.alternative_slots) <= 3
    # One range SearchAvailability — not a 30-minute walk of Square.
    assert len(transport.calls_to("/v2/bookings/availability/search")) == 1
    # Every offered slot's identifiers came straight from a Square response.
    for slot in draft.alternative_slots:
        assert slot["service_variation_id"] == "var_dt_60"
        assert slot["team_member_id"] == "TM1"

    # Only availability-search calls were made — no customer/booking writes.
    assert transport.calls_to("/v2/customers") == []


async def test_earliest_request_with_no_opening_in_window_is_a_conflict_not_an_error(monkeypatch):
    spa = _spa()
    adapter = SpaBookingAdapter(spa)
    transport = _FakeTransport()  # nothing registered -> Square returns no availabilities anywhere
    monkeypatch.setattr(adapter.delegate, "_request", transport)
    _wire(monkeypatch, spa, adapter)

    intent = AppointmentIntent(
        intent="schedule", confidence=1.0, service_description="Deep tissue massage", earliest=True,
    )
    session = _session("call-earliest-none", spa, history=list(_EARLIEST_TURN))
    result = await stage_booking(_FakeDB(), session, intent)

    assert result.outcome is BookingOutcome.CONFLICT
    assert "no longer available" not in result.message.lower()


# --------------------------------------------------------------------------- #
# I/J. HydroLux5 variation ambiguity
# --------------------------------------------------------------------------- #
async def test_generic_hydrolux5_requires_clarification_with_zero_square_availability_calls(
    monkeypatch,
):
    spa = _spa(services=_HYDROLUX5_SERVICES)
    adapter = SpaBookingAdapter(spa)
    transport = _FakeTransport()
    monkeypatch.setattr(adapter.delegate, "_request", transport)
    _wire(monkeypatch, spa, adapter)

    intent = AppointmentIntent(
        intent="schedule", confidence=1.0,
        service_description="HydroLux5",
        requested_start_iso="2026-09-25T14:00:00",
    )
    result = await stage_booking(_FakeDB(), _session("call-hydro-ambiguous", spa), intent)

    assert result.outcome is BookingOutcome.MISSING_INFO
    assert "matches more than one service" in result.message
    assert "Face Only" in result.message and "Face + Neck" in result.message
    assert transport.calls_to("/v2/bookings/availability/search") == []


async def test_explicit_hydrolux5_face_only_proceeds_once_time_is_also_given(monkeypatch):
    spa = _spa(services=_HYDROLUX5_SERVICES)
    adapter = SpaBookingAdapter(spa)
    transport = _FakeTransport()
    start_at = "2026-09-25T14:00:00Z"
    transport.availability_by_start[start_at] = [
        {
            "start_at": start_at,
            "location_id": "loc_123",
            "appointment_segments": [
                {
                    "team_member_id": "TM1",
                    "service_variation_id": "var_face_only",
                    "service_variation_version": 3,
                    "duration_minutes": 45,
                }
            ],
        }
    ]
    monkeypatch.setattr(adapter.delegate, "_request", transport)
    _wire(monkeypatch, spa, adapter)

    intent = AppointmentIntent(
        intent="schedule", confidence=1.0,
        service_description="HydroLux5 Facial - Face Only",
        requested_start_iso="2026-09-25T14:00:00",
    )
    result = await stage_booking(_FakeDB(), _session("call-hydro-face-only", spa), intent)

    assert result.outcome is BookingOutcome.DRAFT, result.message
    assert transport.calls_to("/v2/bookings/availability/search") != []
