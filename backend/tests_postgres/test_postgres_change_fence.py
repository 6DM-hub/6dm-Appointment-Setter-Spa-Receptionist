"""R3-F4/F5 on a REAL PostgreSQL: the write-ahead change fence.

The provider (Square) is SIMULATED (FakeSquare via Codex's tenant helpers);
the database, transactions, connections and advisory locks are real. Opt-in,
like test_postgres_booking_races.py: set CARA_TEST_POSTGRES_URL to a
disposable database. Every test wipes all tables.
"""
from __future__ import annotations

import asyncio
import copy
import os
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import BookingProvider, SpaAccount
from app.models.appointment import CardStatus
from app.models.base import Base
from app.models.booking_escalation import BookingEscalation
from app.services import appointment_booking_service as booking
from app.services import booking_change_guard as guard
from app.services.booking_adapters.base import BookingProviderError
from app.services.booking_escalations import open_change_fence
from app.services.booking_state import arm_verified_proposal
from app.services.grok_service import AppointmentIntent
from tests.test_brick1_booking_change_guard import T3, local
from tests.test_codex_brick1_tenant_boundaries import book, session_for, setup_tenants

URL = os.environ.get("CARA_TEST_POSTGRES_URL")
if not URL:
    raise RuntimeError("Set CARA_TEST_POSTGRES_URL to a disposable PostgreSQL database")


@pytest_asyncio.fixture
async def world(monkeypatch):
    engine = create_async_engine(URL, pool_size=10)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for name in ("_notify_staff_event", "_notify_provider_error", "upsert_external_customer_link"):
        monkeypatch.setattr(booking, name, AsyncMock(return_value=None))
    monkeypatch.setattr(booking, "card_status_for_booked_appointment",
                        AsyncMock(return_value=CardStatus.NOT_REQUIRED))
    async with maker() as db:
        spa = SpaAccount(
            id=uuid.uuid4(), name="Rubyz", timezone="America/Chicago",
            twilio_phone_number="+15550001000", booking_provider=BookingProvider.GOOGLE_CALENDAR,
            booking_config={}, booking_policies={},
            business_hours={d: [{"open": "09:00", "close": "18:00"}]
                            for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
            services=[{"name": "Signature Facial", "duration_minutes": 60}],
            staff=[{"name": "Therapist 0", "services": []}],
        )
        db.add(spa)
        await db.commit()
        db_, a, b, squares = await setup_tenants((db, spa), monkeypatch)
        call = session_for(a, "pg-fence-book")
        row = await book(db, a, call)
        yield SimpleNamespace(maker=maker, spa=a, square=squares["fake-cleaning-token"],
                              squares=squares, row_id=row.id, scope=call.scope, booked=call)
    await engine.dispose()


def _writes(square):
    return len([c for c in square.calls if c[0] == "PUT" or c[1].endswith("/cancel")])


async def _fences(maker, appointment_id):
    async with maker() as fresh:   # a NEW connection: only committed rows are visible
        rows = (await fresh.execute(select(BookingEscalation))).scalars().all()
        return [r for r in rows if (r.details or {}).get("appointment_id") == str(appointment_id)]


async def test_interrupted_move_is_fenced_and_blocks_the_next_call(world):
    w = world
    w.square.open_all(T3)
    square = w.square

    async def interrupted(method, route, *, json=None):
        result = await square(method, route, json=json)
        if method == "PUT":
            raise asyncio.CancelledError("simulated worker interrupted after the provider applied the move")
        return result

    w.squares["fake-cleaning-token"] = interrupted
    first = w.booked   # the call that made the booking moves it (as in Codex's R3 case)
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
    [fence] = await _fences(w.maker, w.row_id)
    assert fence.details["stage"] == "in_flight" and fence.status == "pending"

    w.squares["fake-cleaning-token"] = square
    before = _writes(square)
    later = session_for(w.spa, "pg-next-call")
    async with w.maker() as db:
        later.add_turn("user", "Please cancel my appointment.")
        await booking.cancel_booking(db, later)
        if guard.PENDING_CANCELLATION in later.entities:
            later.add_turn("user", "Yes.")
            await booking.cancel_booking(db, later)
    assert _writes(square) == before


async def test_dashboard_unknown_outcome_blocks_a_later_dashboard_change(world):
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
    [fence] = await _fences(w.maker, w.row_id)
    assert fence.details["stage"] == "unknown_outcome" and fence.delivery_status == "pending"
    before = _writes(square)
    async with w.maker() as db:
        with pytest.raises(HTTPException) as err:
            await cancel_appointment(w.row_id, scope=w.scope, db=db)
        assert err.value.status_code == 409
    assert _writes(square) == before


async def test_opening_a_fence_keeps_the_request_transaction_and_its_locks(world):
    w = world
    call_key = "booking-call:pg-lock-holder"
    async with w.maker() as db:
        await booking._lock_call(db, "pg-lock-holder")
        fence_id = await open_change_fence(db, w.spa.id, "pg-lock-holder",
                                           {"appointment_id": str(w.row_id), "operation": "move"})
        assert fence_id
        async with w.maker() as other:
            held = (await other.execute(text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": call_key})).scalar()
            assert held is False, "opening the fence released this request's advisory lock"
            await other.rollback()
        assert len(await _fences(w.maker, w.row_id)) == 1   # committed independently
        await db.rollback()
    assert len(await _fences(w.maker, w.row_id)) == 1       # survives the request's rollback


async def _slow_square(w, delay=0.3):
    square = w.square

    async def slow(method, route, *, json=None):
        if method == "PUT" or (method == "POST" and route.endswith("/cancel")):
            await asyncio.sleep(delay)
        return await square(method, route, json=json)

    w.squares["fake-cleaning-token"] = slow


async def test_two_concurrent_dashboard_cancels_send_one_write(world):
    from app.api.v1.appointments import cancel_appointment
    w = world
    await _slow_square(w)
    before = _writes(w.square)

    async def attempt():
        async with w.maker() as db:
            try:
                await cancel_appointment(w.row_id, scope=w.scope, db=db)
                return 200
            except HTTPException as exc:
                return exc.status_code

    codes = sorted(await asyncio.gather(attempt(), attempt()))
    assert _writes(w.square) - before == 1, ("TWO PROVIDER WRITES", codes, w.square.calls)
    assert codes == [200, 409], codes   # one succeeds, the other is refused


async def test_voice_and_dashboard_racing_send_one_write(world):
    from app.api.v1.appointments import update_appointment
    from app.schemas import AppointmentUpdate
    w = world
    w.square.open_all(T3)
    caller = session_for(w.spa, "pg-voice-race")
    async with w.maker() as db:
        caller.add_turn("user", "Please cancel my appointment.")
        asked = await booking.cancel_booking(db, caller)
        assert guard.PENDING_CANCELLATION in caller.entities, asked.message
    caller.add_turn("user", "Yes.")
    await _slow_square(w)
    before = _writes(w.square)

    async def voice():
        async with w.maker() as db:
            return (await booking.cancel_booking(db, caller)).outcome

    async def dashboard():
        async with w.maker() as db:
            try:
                await update_appointment(w.row_id, AppointmentUpdate(start_time=T3, end_time=T3 + timedelta(hours=1)),
                                         scope=w.scope, db=db)
                return 200
            except HTTPException as exc:
                return exc.status_code

    voice_outcome, dashboard_code = await asyncio.gather(voice(), dashboard())
    assert _writes(w.square) - before == 1, ("TWO PROVIDER WRITES", voice_outcome, dashboard_code)
    # Exactly one of them went through; the other was refused before sending.
    assert (voice_outcome == booking.BookingOutcome.ERROR) != (dashboard_code == 409), (
        voice_outcome, dashboard_code)
