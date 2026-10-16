"""R2: behaviour-first PostgreSQL tests that depend on the write-ahead fence.

The existing fence tests read the fence row before checking behaviour, so with
the fence removed (pg_controls/fence_removed) they stop at "no row"
(ValueError / KeyError) instead of showing the safety failure. These tests
assert the SAFETY OUTCOME first: after a change whose provider outcome was not
recorded (worker interrupted after the provider applied it, or a lost reply),
a later change of the same appointment from ANOTHER call or the dashboard must
not reach the provider. The slot lock cannot provide that (the requests are
not concurrent), so only the durable fence does. With the fence removed these
fail with an AssertionError naming the second provider write.

SIMULATED provider (FakeSquare); real database, transactions and connections.
"""
import asyncio
import copy

import pytest
from fastapi import HTTPException

from app.services import appointment_booking_service as booking
from app.services import booking_change_guard as guard
from app.services.booking_adapters.base import BookingProviderError
from app.services.booking_state import arm_verified_proposal
from app.services.grok_service import AppointmentIntent
from tests.test_brick1_booking_change_guard import T3, local
from tests.test_codex_brick1_tenant_boundaries import session_for
from tests_postgres.test_postgres_change_fence import _writes, world  # noqa: F401  (fixture)


async def _interrupted_move(w):
    """The booking call moves its appointment; the worker dies right after the
    provider applied the move, before anything local was committed."""
    w.square.open_all(T3)
    square = w.square

    async def interrupted(method, route, *, json=None):
        result = await square(method, route, json=json)
        if method == "PUT":
            raise asyncio.CancelledError("simulated worker interrupted after the provider applied the move")
        return result

    w.squares["fake-cleaning-token"] = interrupted
    first = w.booked
    async with w.maker() as db:
        first.add_turn("user", "Move my appointment to Thursday at 3 PM instead.")
        staged = await booking.stage_booking(db, first, AppointmentIntent(
            intent="reschedule", confidence=1, service_description=w.spa.services[0]["name"],
            requested_start_iso=local(T3)))
        assert staged.outcome == booking.BookingOutcome.DRAFT, staged.message
        arm_verified_proposal(first)
        with pytest.raises(asyncio.CancelledError):
            await booking.confirm_booking(db, first)
        await db.rollback()
    w.squares["fake-cleaning-token"] = square
    return square


async def test_after_an_interrupted_move_another_call_cannot_send_a_second_write(world):
    w = world
    square = await _interrupted_move(w)
    before = _writes(square)
    later = session_for(w.spa, "pg-behaviour-next-call")
    async with w.maker() as db:
        later.add_turn("user", "Please cancel my appointment.")
        await booking.cancel_booking(db, later)
        if guard.PENDING_CANCELLATION in later.entities:
            later.add_turn("user", "Yes.")
            await booking.cancel_booking(db, later)
    second = _writes(square) - before
    assert second == 0, ("TWO PROVIDER WRITES: a later call changed an appointment whose earlier "
                         "change outcome was never recorded", second, square.calls[-3:])


async def test_after_an_interrupted_move_the_dashboard_cannot_send_a_second_write(world):
    from app.api.v1.appointments import cancel_appointment
    w = world
    square = await _interrupted_move(w)
    before = _writes(square)
    async with w.maker() as db:
        try:
            await cancel_appointment(w.row_id, scope=w.scope, db=db)
        except HTTPException as exc:
            assert exc.status_code == 409, exc.detail
    second = _writes(square) - before
    assert second == 0, ("TWO PROVIDER WRITES: the dashboard changed an appointment whose earlier "
                         "change outcome was never recorded", second, square.calls[-3:])


async def test_after_a_lost_dashboard_cancel_a_retry_cannot_send_a_second_write(world):
    from app.api.v1.appointments import cancel_appointment
    w = world
    square = w.square

    async def lose(method, route, *, json=None):
        if method == "POST" and route.endswith("/cancel"):
            square.calls.append((method, route, copy.deepcopy(json)))
            raise BookingProviderError("lost cancellation response", code="BOOKING_OUTCOME_UNKNOWN")
        return await square(method, route, json=json)

    w.squares["fake-cleaning-token"] = lose
    async with w.maker() as db:
        with pytest.raises(HTTPException) as err:
            await cancel_appointment(w.row_id, scope=w.scope, db=db)
        assert err.value.status_code == 502
    w.squares["fake-cleaning-token"] = square
    before = _writes(square)
    async with w.maker() as db:
        try:
            await cancel_appointment(w.row_id, scope=w.scope, db=db)
        except HTTPException as exc:
            assert exc.status_code == 409, exc.detail
    second = _writes(square) - before
    assert second == 0, ("TWO PROVIDER WRITES: a retried dashboard cancel was sent although the "
                         "first one's outcome is unknown", second, square.calls[-3:])
