"""Claude reproduction for F1 (NOT part of any commit). SIMULATED providers only.

Path: the realtime tool wrapper ``_invoke_tool_with_hold`` runs
propose_appointment under ``asyncio.wait_for(..., timeout=20)``. If staging is
still inside ``stage_booking`` when that fires, the handler is CANCELLED (not
failed), so ``apply_booking_result`` never runs: ``booking_status`` stays
"booked" while the draft already holds the new, never-read-back time. A later
confirm_appointment then takes the ``already_booked`` bypass.

The 20 s timeout is shortened to 0.2 s and one staging step is made to hang;
nothing else is changed. Each test asserts the REQUIRED behaviour (no provider
move without a fresh read-back and yes), so on a vulnerable commit it FAILS.
"""
import asyncio

import pytest

from app.services import appointment_booking_service as booking
from app.services import xai_realtime
from app.services.booking_state import get_draft
from tests.test_brick1_booking_change_guard import T3, Z2, booked_call, local, make_world

_REAL_WAIT_FOR = asyncio.wait_for


@pytest.mark.parametrize("trigger", ["model_confirm_tool", "caller_yes_then_model_confirm"])
@pytest.mark.parametrize("boundary", ["_prepare", "persist_caller_identity"])
@pytest.mark.parametrize("business", ["spa", "hvac", "dental"])
async def test_timed_out_stage_cannot_move_booking_without_fresh_yes(monkeypatch, business, boundary, trigger):
    world = make_world(monkeypatch, business)
    phone = await booked_call(world)
    await phone.says("Move my appointment to Thursday at 3 PM instead.")

    original = getattr(booking, boundary)
    hung = []

    async def hang(*_a, **_k):
        hung.append(boundary)
        await asyncio.sleep(3600)

    async def short_wait_for(aw, timeout=None):
        return await _REAL_WAIT_FOR(aw, 0.2 if timeout == 20 else timeout)

    monkeypatch.setattr(booking, boundary, hang)
    monkeypatch.setattr(xai_realtime.asyncio, "wait_for", short_wait_for)
    result = await phone.propose(
        operation="reschedule", requested_start_iso=local(T3),
        service_description=world.first, caller_name="Ada Lovelace",
    )
    monkeypatch.setattr(booking, boundary, original)
    monkeypatch.setattr(xai_realtime.asyncio, "wait_for", _REAL_WAIT_FOR)
    assert hung, f"staging never reached {boundary}: {result}"

    state = {
        "propose_result": result,
        "booking_status_after_timeout": phone.session.booking_status,
        "draft_start_after_timeout": get_draft(phone.session).start_iso,
        "draft_persisted": get_draft(phone.session).is_persisted,
    }
    heard_before = len(phone.heard)
    if trigger == "caller_yes_then_model_confirm":
        await phone.says("Yes.")   # e.g. answering the callback question
    confirm = await phone.tool("confirm_appointment")

    diagnostics = {
        **state,
        "confirm_result": confirm,
        "square_updates": world.square.updates,
        "square_active_starts": phone.starts(),
        "heard_after_timeout": phone.heard[heard_before:],
    }
    assert not world.square.updates, f"PROVIDER MOVED WITHOUT FRESH CONSENT: {diagnostics}"
    assert phone.starts() == [Z2], diagnostics
