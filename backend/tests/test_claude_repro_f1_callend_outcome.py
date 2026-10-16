"""Claude reproduction (NOT part of any commit). SIMULATED providers only.

Side effect check for the F1 repair: at call end, ``_settle_inflight_tools``
cancels a still-running tool. If that tool is propose_appointment for a caller
who ALREADY has a confirmed booking, the provider booking is unchanged, so the
call's recorded outcome (``session.booking_status``, written to
call_log.ai_analysis["booking_outcome"] by finalization) must still say the
call booked. Nothing here asserts consent behaviour; the 12-case F1 repro
covers that.
"""
import asyncio

import pytest

from app.services import appointment_booking_service as booking
from tests.test_brick1_booking_change_guard import T2, T3, Z2, booked_call, local, make_world


@pytest.mark.parametrize("request_kind", ["move_request", "same_booking_again"])
@pytest.mark.parametrize("business", ["spa", "hvac", "dental"])
async def test_hangup_during_staging_keeps_booked_outcome(monkeypatch, business, request_kind):
    world = make_world(monkeypatch, business)
    phone = await booked_call(world)
    assert phone.session.booking_status in {"booked", "rescheduled"}

    async def hang(*_a, **_k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(booking, "_prepare", hang)
    if request_kind == "move_request":
        await phone.says("Move my appointment to Thursday at 3 PM instead.")
        args = dict(operation="reschedule", requested_start_iso=local(T3))
    else:
        args = dict(operation="schedule", requested_start_iso=local(T2))
    task = asyncio.create_task(phone.propose(service_description=world.first,
                                             caller_name="Ada Lovelace", **args))
    phone.voice._inflight_tools.add(task)
    await asyncio.sleep(0.05)                       # the tool is now inside staging
    monkeypatch.setattr(type(phone.voice), "TOOL_DRAIN_TIMEOUT_SECONDS", 0.05, raising=False)
    await phone.voice._settle_inflight_tools()      # what call end does

    diagnostics = {"booking_status_at_finalization": phone.session.booking_status,
                   "square_active_starts": phone.starts(), "square_updates": world.square.updates}
    assert phone.starts() == [Z2] and not world.square.updates, diagnostics
    assert phone.session.booking_status in {"booked", "rescheduled"}, (
        f"CALL WOULD BE RECORDED AS NOT BOOKED although the booking exists: {diagnostics}")
