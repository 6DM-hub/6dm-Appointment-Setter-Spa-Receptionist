"""Failed staging must not authorize a realtime provider move. Simulated only."""
import pytest

from app.services import appointment_booking_service as booking
from app.services.booking_state import get_draft
from tests.test_brick1_booking_change_guard import (
    T3, Z2, booked_call, local, make_world, reconnect,
)


@pytest.mark.parametrize("boundary", ["_prepare", "persist_caller_identity"])
@pytest.mark.parametrize("business", ["spa", "hvac", "dental"])
@pytest.mark.parametrize("restored", [False, True])
async def test_failed_realtime_stage_cannot_move_without_fresh_yes(monkeypatch, boundary, business, restored):
    world = make_world(monkeypatch, business)
    phone = await booked_call(world)
    await phone.says("Move my appointment to Thursday at 3 PM instead.")
    original = getattr(booking, boundary)
    failures = []

    async def fail(*args, **kwargs):
        failures.append(boundary)
        raise RuntimeError("isolated staging failure")

    monkeypatch.setattr(booking, boundary, fail)
    result = await phone.propose(
        operation="reschedule", requested_start_iso=local(T3),
        service_description=world.first, caller_name="Ada Lovelace",
    )
    assert failures, result
    assert result["status"] == "error", result
    assert phone.session.booking_status == "error"
    assert get_draft(phone.session).start_iso == local(T3)
    monkeypatch.setattr(booking, boundary, original)
    if restored:
        phone = reconnect(phone)
    # A model tool call is not caller consent. No new read-back or yes occurred.
    await phone.tool("confirm_appointment")
    assert not world.square.updates, "provider moved after failed staging without consent"
    assert phone.starts() == [Z2]
