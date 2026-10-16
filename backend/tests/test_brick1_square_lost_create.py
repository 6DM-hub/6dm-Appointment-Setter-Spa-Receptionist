"""Lost create replies through the real Square HTTP error mapping, simulated only."""
import copy
import json

import httpx
import pytest

from app.services.booking_adapters.base import BookingProviderError
from app.services.booking_adapters.providers.square import SquareAdapter
from app.services.square_booking_recovery import create_with_recovery


def request_payload():
    return {"idempotency_key": "stable-intent-123", "booking": {
        "location_id": "location-one", "customer_id": "customer-one",
        "start_at": "2026-11-01T16:00:00Z", "appointment_segments": [{
            "team_member_id": "staff-one", "service_variation_id": "service-one",
            "duration_minutes": 60, "service_variation_version": 1}]}}


def install_transport(monkeypatch, fault, mismatch=None):
    payload = request_payload()
    stored = {**copy.deepcopy(payload["booking"]), "id": "booking-one", "status": "ACCEPTED"}
    calls = []
    writes = []
    real_client = httpx.AsyncClient

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            writes.append(json.loads(request.content))
            assert writes[-1] == payload
            if len(writes) == 1 or mismatch == "replay_timeout":
                if fault == "timeout":
                    raise httpx.ReadTimeout("reply lost after storage", request=request)
                if fault == "server_error":
                    return httpx.Response(503, json={"errors": [{"code": "INTERNAL_SERVER_ERROR"}]})
                return httpx.Response(200, content=b"not-json")
            if mismatch == "replay_forbidden":
                return httpx.Response(403, json={"errors": [{"code": "FORBIDDEN"}]})
            replay = copy.deepcopy(stored)
            if mismatch == "replay_wrong_customer":
                replay["customer_id"] = "another-customer"
            return httpx.Response(200, json={"booking": replay})
        assert request.url.path == "/v2/bookings/booking-one"
        current = copy.deepcopy(stored)
        if mismatch == "read_timeout":
            raise httpx.ReadTimeout("read unavailable", request=request)
        if mismatch in {"id", "location_id", "customer_id", "start_at", "status"}:
            current[mismatch] = {"start_at": "2026-11-01T17:00:00Z", "status": "CANCELLED_BY_CUSTOMER"}.get(mismatch, "different")
        if mismatch in {"team_member_id", "service_variation_id", "duration_minutes", "service_variation_version"}:
            current["appointment_segments"][0][mismatch] = "different"
        return httpx.Response(200, json={"booking": current})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    return payload, calls, writes


@pytest.mark.parametrize("industry", ["spa", "hvac", "dental"])
@pytest.mark.parametrize("fault", ["timeout", "server_error", "malformed"])
async def test_lost_create_recovers_exact_key_and_current_booking(monkeypatch, industry, fault):
    payload, calls, writes = install_transport(monkeypatch, fault)
    adapter = SquareAdapter(industry, {"access_token": "test-only", "location_id": "location-one"})
    result = await create_with_recovery(adapter, payload)
    assert result["booking"]["id"] == "booking-one"
    assert writes == [payload, payload]
    assert calls == [("POST", "/v2/bookings"), ("POST", "/v2/bookings"), ("GET", "/v2/bookings/booking-one")]


@pytest.mark.parametrize("mismatch", ["id", "location_id", "customer_id", "start_at", "status", "team_member_id", "service_variation_id", "duration_minutes", "service_variation_version", "read_timeout", "replay_timeout", "replay_forbidden", "replay_wrong_customer"])
async def test_recovery_requires_verified_current_booking_and_is_bounded(monkeypatch, mismatch):
    payload, calls, writes = install_transport(monkeypatch, "timeout", mismatch)
    adapter = SquareAdapter("business", {"access_token": "test-only", "location_id": "location-one"})
    with pytest.raises(BookingProviderError) as error:
        await create_with_recovery(adapter, payload)
    assert error.value.code == "BOOKING_OUTCOME_UNKNOWN"
    assert writes == [payload, payload]
    assert len(calls) <= 4  # GET retries once inside the real HTTP adapter.


@pytest.mark.parametrize("industry", ["spa", "hvac", "dental"])
async def test_realtime_lost_reply_saves_one_local_and_one_square_booking(monkeypatch, industry):
    from tests.test_brick1_booking_change_guard import make_world, Phone, T2, Z2, local

    world = make_world(monkeypatch, industry)
    delegate = world.adapter.delegate
    monkeypatch.setattr(delegate, "_request", SquareAdapter._request.__get__(delegate, SquareAdapter))
    real_client = httpx.AsyncClient
    creates = []

    async def handle(request):
        body = json.loads(request.content) if request.content else None
        result = await world.square(request.method, request.url.path, json=body)
        if request.method == "POST" and request.url.path == "/v2/bookings":
            creates.append(copy.deepcopy(body))
            if len(creates) == 1:
                raise httpx.ReadTimeout("stored, reply lost", request=request)
        return httpx.Response(200, json=result)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    phone = Phone(world)
    phone.voice._pending_caller = f"I'd like a {world.first} on Thursday at 2 PM. My name is Ada Lovelace."
    phone.voice._flush_caller_turn()
    proposal = await phone.propose(requested_start_iso=local(T2), service_description=world.first,
                                   caller_name="Ada Lovelace", operation="schedule")
    assert proposal["status"] == "draft", proposal
    await phone.says("Yes, please.")
    assert phone.starts() == [Z2], phone.heard
    assert len(creates) == 2 and creates[0] == creates[1]
    assert len(world.square.bookings) == 1
    assert len(world.store.live) == 1
    assert world.store.live[0].external_booking_id == world.square.active()[0]["id"]
