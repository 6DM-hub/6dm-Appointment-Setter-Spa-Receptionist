"""F1-R1: a call that booked is still recorded as booked when the caller
started a change and hung up, without re-opening any permission to move it.

SIMULATED: the real realtime handler, booking engine and FakeSquare
(tests/square_world.py). The reported outcome is what both finalizers write to
call_log.ai_analysis["booking_outcome"] (booking_guard.reported_booking_outcome).
"""
import asyncio

import pytest

from app.services import appointment_booking_service as booking
from app.services import booking_change_guard as guard
from app.services.booking_state import get_draft
from tests.test_brick1_booking_change_guard import T3, Z2, booked_call, local, make_world


async def _hang_up_during_staging(monkeypatch, phone, world, **args):
    async def hang(*_a, **_k):
        await asyncio.sleep(3600)

    original = booking._prepare
    monkeypatch.setattr(booking, "_prepare", hang)
    task = asyncio.create_task(phone.propose(service_description=world.first,
                                             caller_name="Ada Lovelace", **args))
    phone.voice._inflight_tools.add(task)
    await asyncio.sleep(0.05)
    monkeypatch.setattr(type(phone.voice), "TOOL_DRAIN_TIMEOUT_SECONDS", 0.05, raising=False)
    await phone.voice._settle_inflight_tools()
    monkeypatch.setattr(booking, "_prepare", original)


@pytest.mark.parametrize("business", ["spa", "hvac", "dental"])
async def test_abandoned_move_is_reported_booked_and_cannot_be_confirmed_later(monkeypatch, business):
    world = make_world(monkeypatch, business)
    phone = await booked_call(world)
    await phone.says("Move my appointment to Thursday at 3 PM instead.")
    await _hang_up_during_staging(monkeypatch, phone, world,
                                  operation="reschedule", requested_start_iso=local(T3))
    assert guard.reported_booking_outcome(phone.session) == "booked"
    assert phone.session.entities.get("abandoned_change_at_call_end") is True
    # The session is back on the booking the calendar holds: 2 PM, not 3 PM.
    snapshot = phone.session.entities[guard.BOOKED_DRAFT_SNAPSHOT]["draft"]
    assert get_draft(phone.session).start_iso == snapshot["start_iso"]
    # A late confirmation (e.g. a replayed model tool call) cannot move it.
    await phone.tool("confirm_appointment")
    assert phone.starts() == [Z2] and not world.square.updates


async def test_abandoned_change_after_staging_completed_is_reported_booked(monkeypatch):
    """Legacy-style end state: the change was staged (awaiting consent) when
    the caller hung up. Nothing changed at the provider."""
    world = make_world(monkeypatch, "spa")
    phone = await booked_call(world)
    await phone.says("Move my appointment to Thursday at 3 PM instead.")
    await phone.propose(operation="reschedule", requested_start_iso=local(T3),
                        service_description=world.first, caller_name="Ada Lovelace")
    assert phone.session.booking_status not in {"booked", "rescheduled"}
    assert guard.reported_booking_outcome(phone.session) == "booked"
    assert phone.starts() == [Z2] and not world.square.updates


async def test_cancelled_booking_is_never_reported_or_restored_as_booked(monkeypatch):
    world = make_world(monkeypatch, "spa")
    phone = await booked_call(world)
    session = phone.session
    # A completed in-call cancellation clears the active pointer and records it.
    session.entities.pop("active_appointment_id", None)
    session.entities["last_booking_outcome"] = "cancelled"
    session.booking_status = "collecting_details"
    assert guard.reported_booking_outcome(session) == "collecting_details"
    assert guard.settle_abandoned_change(session) is False
    assert session.booking_status == "collecting_details"


async def test_unknown_change_outcome_is_not_reported_booked(monkeypatch):
    world = make_world(monkeypatch, "spa")
    phone = await booked_call(world)
    session = phone.session
    session.booking_status = "error"
    monkeypatch.setattr(guard, "reconciliation_pending", lambda _s: True)
    assert guard.reported_booking_outcome(session) == "error"
    assert guard.settle_abandoned_change(session) is False


async def test_no_booking_and_completed_booking_outcomes_unchanged(monkeypatch):
    world = make_world(monkeypatch, "spa")
    phone = await booked_call(world)
    assert guard.reported_booking_outcome(phone.session) == phone.session.booking_status
    from tests.square_world import call
    fresh = call(world.spa)
    assert guard.reported_booking_outcome(fresh) == "no_booking"
