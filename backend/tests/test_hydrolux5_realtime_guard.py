"""Phase 1, item #7: a generic "HydroLux5" request must not let the model
route around service-variation clarification by calling the realtime
`propose_appointment` tool directly. This exercises the actual
`XAIVoiceSession._run_propose_appointment` -> `stage_booking` ->
`SpaBookingAdapter.resolve_service` path, not just the backend function in
isolation (see `tests/test_temporal_grounding_backend.py` for that), to prove
the realtime wiring doesn't bypass it.
"""
import json

import pytest

from app.models import BookingProvider
from app.services import appointment_booking_service, xai_realtime
from app.services.booking_adapters import SpaBookingAdapter
from app.services.call_state import CallSession
from tests.conftest import make_spa

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

_BUSINESS_HOURS_ALWAYS_OPEN = {
    day: [{"open": "00:00", "close": "23:59"}]
    for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
}


class _RejectTransport:
    """Any Square call at all is a failure for this test — clarification
    must happen before the catalog/availability layer is ever reached."""

    async def __call__(self, method, path, *, json=None):
        raise AssertionError(f"Square must not be called: {method} {path}")


class _AllowExactTimeTransport:
    """Echoes back whatever exact start the caller-grounded request asked
    for, as a real (fake) Square response would."""

    async def __call__(self, method, path, *, json=None):
        if str(path).startswith("/v2/locations/"):
            return {
                "location": {
                    "id": "loc_123",
                    "status": "ACTIVE",
                    "name": "Test",
                    "timezone": "UTC",
                }
            }
        if path == "/v2/bookings/availability/search":
            start_at = json["query"]["filter"]["start_at_range"]["start_at"]
            return {
                "availabilities": [
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
            }
        raise AssertionError(f"unexpected Square path: {path}")


class _FakeDB:
    def add(self, _obj):
        return None

    async def rollback(self):
        return None


class _FakeSessionCtx:
    async def __aenter__(self):
        return _FakeDB()

    async def __aexit__(self, *exc):
        return False


async def _async_value(value):
    return value


def _make_voice_session(monkeypatch, transport) -> "xai_realtime.XAIVoiceSession":
    spa = make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "t", "location_id": "loc_123"},
        business_hours=_BUSINESS_HOURS_ALWAYS_OPEN,
        timezone="UTC",
        services=_HYDROLUX5_SERVICES,
    )
    session = CallSession(
        "call-hydro-rt", "inbound", "+15550000002", "+15550000001", tenant_id=str(spa.id),
        history=[{"role": "assistant", "content": "Thank you for calling."}],
    )
    voice = xai_realtime.XAIVoiceSession("call-hydro-rt", session)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(voice, "_persist_session", _noop)

    adapter = SpaBookingAdapter(spa)
    monkeypatch.setattr(adapter.delegate, "_request", transport)

    monkeypatch.setattr(xai_realtime, "AsyncSessionLocal", _FakeSessionCtx)
    monkeypatch.setattr(appointment_booking_service, "_load_spa", lambda *_: _async_value(spa))
    monkeypatch.setattr(appointment_booking_service, "get_booking_adapter", lambda **_: adapter)
    monkeypatch.setattr(appointment_booking_service, "persist_caller_identity", _noop)
    monkeypatch.setattr(appointment_booking_service, "_has_conflict", lambda *_a, **_k: _async_value(False))

    return voice


async def test_generic_hydrolux5_via_propose_appointment_tool_asks_for_clarification(monkeypatch):
    voice_session = _make_voice_session(monkeypatch, _RejectTransport())

    output = await voice_session._run_propose_appointment(
        json.dumps({
            "service_description": "HydroLux5",
            "requested_start_iso": "2026-09-25T14:00:00",
        })
    )
    payload = json.loads(output)

    assert payload["status"] == "missing_info"
    assert payload["booked"] is False
    assert "Face Only" in payload["message"]
    assert "Face + Neck" in payload["message"]


async def test_explicit_hydrolux5_variation_via_propose_appointment_tool_is_not_blocked(monkeypatch):
    """Once the caller has actually named a variation, the same tool call
    must proceed normally — the guard only blocks the ambiguous case."""
    voice_session = _make_voice_session(monkeypatch, _AllowExactTimeTransport())

    output = await voice_session._run_propose_appointment(
        json.dumps({
            "service_description": "HydroLux5 Facial - Face Only",
            "requested_start_iso": "2026-09-25T14:00:00",
        })
    )
    payload = json.loads(output)

    assert payload["status"] == "draft", payload
