"""F1 consent regressions through real voice paths; simulated Square only."""
import asyncio
from datetime import timedelta

import pytest

from app.services import appointment_booking_service as booking
from app.services import booking_change_guard as guard
from app.services import xai_realtime
from app.services.booking_state import confirmation_block_reason, get_draft, save_draft
from tests.test_brick1_booking_change_guard import (
    BUSINESSES, T3, THREE, Z2, Z3, _intent, _legacy_booked, booked_call,
    make_world, only_the_original, reconnect,
)

REAL_WAIT_FOR = asyncio.wait_for


@pytest.mark.parametrize("business", sorted(BUSINESSES))
@pytest.mark.parametrize("boundary", ["_prepare", "persist_caller_identity"])
@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
async def test_interrupted_realtime_stage_then_reconnect_cannot_reuse_yes(
    monkeypatch, business, boundary, interruption,
):
    world = make_world(monkeypatch, business)
    phone = await booked_call(world)
    await phone.says("Move my appointment to Thursday at 3 PM instead.")
    reached = asyncio.Event()
    original = getattr(booking, boundary)

    async def hang(*args, **kwargs):
        reached.set()
        await asyncio.Future()

    async def short_wait_for(aw, timeout=None):
        return await REAL_WAIT_FOR(aw, 0.2 if timeout == 20 else timeout)

    monkeypatch.setattr(booking, boundary, hang)
    if interruption == "timeout":
        monkeypatch.setattr(xai_realtime.asyncio, "wait_for", short_wait_for)
        await phone.propose(operation="reschedule", **THREE(world))
    else:
        task = asyncio.create_task(phone.propose(operation="reschedule", **THREE(world)))
        try:
            await REAL_WAIT_FOR(reached.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert reached.is_set()
    monkeypatch.setattr(booking, boundary, original)
    monkeypatch.setattr(xai_realtime.asyncio, "wait_for", REAL_WAIT_FOR)

    restored = reconnect(phone)
    await restored.says("Yes.")  # callback offer, not a booking read-back
    await restored.tool("confirm_appointment")
    only_the_original(restored)


@pytest.mark.parametrize("business", sorted(BUSINESSES))
@pytest.mark.parametrize("boundary", ["_prepare", "persist_caller_identity"])
async def test_cancelled_legacy_staging_does_not_reuse_old_consent(monkeypatch, business, boundary):
    world = make_world(monkeypatch, business)
    phone = await _legacy_booked(world, monkeypatch)
    original = getattr(booking, boundary)
    reached = asyncio.Event()

    async def hang(*args, **kwargs):
        reached.set()
        await asyncio.Future()

    monkeypatch.setattr(booking, boundary, hang)
    task = asyncio.create_task(phone.says(
        "Move my appointment to Thursday at 3 PM instead.",
        intent=_intent("reschedule", T3, world.first),
    ))
    try:
        await REAL_WAIT_FOR(reached.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        monkeypatch.setattr(booking, boundary, original)
    # Legacy saves/reloads JSON on subsequent turns, exercising the shared gate.
    await phone.says("Yes.", reply="Okay.")
    assert phone.starts() == [Z2]
    assert not world.square.updates
    assert len(world.square.creates) == 1


@pytest.mark.parametrize("change", [
    "start_iso", "end_iso", "service_description", "service_id",
    "square_variation_id", "provider_id", "preferred_staff", "missing_snapshot",
])
async def test_booked_flag_cannot_authorize_modified_or_unverifiable_draft(monkeypatch, change):
    world = make_world(monkeypatch)
    phone = await booked_call(world)
    draft = get_draft(phone.session)
    if change == "missing_snapshot":
        phone.session.entities.pop(guard.BOOKED_DRAFT_SNAPSHOT)
    else:
        values = {
            "start_iso": "2026-10-22T15:00:00-05:00",
            "end_iso": "2026-10-22T16:00:00-05:00",
            "service_description": world.second,
            "service_id": "another-service", "square_variation_id": world.second_var,
            "provider_id": "another-provider", "preferred_staff": "Another staff member",
        }
        setattr(draft, change, values[change])
        save_draft(phone.session, draft)
    # Construct the precise stale-state invariant, independent of invalidation.
    assert phone.session.booking_status == "booked"
    assert confirmation_block_reason(phone.session) is not None
    await phone.tool("confirm_appointment")
    only_the_original(phone)


@pytest.mark.parametrize("business", sorted(BUSINESSES))
async def test_exact_repeated_confirmation_after_reconnect_is_noop(monkeypatch, business):
    phone = await booked_call(make_world(monkeypatch, business))
    phone = reconnect(phone)
    assert confirmation_block_reason(phone.session) is None
    await phone.tool("confirm_appointment")
    await phone.tool("confirm_appointment")
    only_the_original(phone)


@pytest.mark.parametrize("stored_change", ["moved", "missing"])
async def test_old_exact_replay_never_writes_when_local_booking_changed(monkeypatch, stored_change):
    world = make_world(monkeypatch)
    phone = await booked_call(world)
    # Another actor updated/deleted the durable record after this call's snapshot.
    # The old approval may acknowledge a no-op, but cannot authorize a write.
    if stored_change == "moved":
        row = world.store.rows[0]
        row.start_time += timedelta(hours=1)
        row.end_time += timedelta(hours=1)
    else:
        world.store.rows.clear()
    await phone.tool("confirm_appointment")
    assert not world.square.updates
    assert len(world.square.creates) == 1


@pytest.mark.parametrize("business", sorted(BUSINESSES))
@pytest.mark.parametrize("path", ["realtime", "legacy"])
async def test_fresh_readback_and_yes_still_reschedules(monkeypatch, business, path):
    world = make_world(monkeypatch, business)
    if path == "realtime":
        phone = await booked_call(world)
        await phone.says("Move my appointment to Thursday at 3 PM instead.")
        result = await phone.propose(operation="reschedule", **THREE(world))
        assert result["status"] == "draft"
        assert not world.square.updates
        phone = reconnect(phone)
        await phone.says("Yes, please.")
    else:
        phone = await _legacy_booked(world, monkeypatch)
        await phone.says(
            "Move my appointment to Thursday at 3 PM instead.",
            intent=_intent("reschedule", T3, world.first),
            reply="Thursday at 3 PM is available. Would you like me to move it?",
        )
        assert not world.square.updates
        await phone.says("Yes, please.", reply="Done.")
    assert phone.starts() == [Z3]
    assert len(world.square.updates) == 1
    assert len(world.square.creates) == 1
