"""Square create attempts survive cancellation at the provider boundary."""
import asyncio
import copy

import pytest

from app.services.booking_state import arm_verified_proposal
from app.services import appointment_booking_service as booking
from tests.test_brick1_booking_change_guard import make_world
from tests.test_brick1_google_g2_pending_create import _pending, _staged_phone


@pytest.mark.parametrize("industry", ["spa", "hvac", "dental"])
async def test_square_attempt_exists_before_send_and_survives_worker_death(monkeypatch, industry):
    world = make_world(monkeypatch, industry)
    phone = await _staged_phone(world)
    delegate = world.adapter.delegate
    original = delegate._request
    at_send = []
    sent = []

    async def interrupted(method, path, **kwargs):
        if method == "POST" and path == "/v2/bookings":
            at_send.append(copy.deepcopy(_pending(world)))
            sent.append(copy.deepcopy(kwargs["json"]))
            await original(method, path, **kwargs)
            raise asyncio.CancelledError("worker died after Square accepted")
        return await original(method, path, **kwargs)

    monkeypatch.setattr(delegate, "_request", interrupted)
    arm_verified_proposal(phone.session)
    with pytest.raises(asyncio.CancelledError):
        await booking.confirm_booking(world.db, phone.session)
    assert len(world.square.bookings) == 1
    assert len(at_send) == 1 and len(at_send[0]) == 1, "NO DURABLE ATTEMPT BEFORE SQUARE WRITE"
    [record] = _pending(world)
    assert record["provider"] == "square" and record["stage"] == "in_flight"
    from app.services.booking_config import decrypt_config
    assert decrypt_config(record["square_request"]) == sent[0]


async def test_square_is_not_sent_when_durable_record_fails(monkeypatch):
    world = make_world(monkeypatch, "hvac")
    phone = await _staged_phone(world)

    async def broken(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(booking, "_open_pending_create", broken)
    arm_verified_proposal(phone.session)
    result = await booking.confirm_booking(world.db, phone.session)
    assert result.outcome is booking.BookingOutcome.ERROR
    assert not world.square.bookings
    assert not list(world.store.live)
