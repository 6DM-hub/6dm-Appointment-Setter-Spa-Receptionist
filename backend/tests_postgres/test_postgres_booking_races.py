"""Booking races against a REAL PostgreSQL, so the advisory locks really run.

Not part of the default suite (pytest.ini collects ``tests/`` only): it needs
a disposable PostgreSQL. Run it with, for example:

    CARA_TEST_POSTGRES_URL='postgresql+asyncpg://postgres@/cara_test?host=/var/tmp/cara-pg/sock' \\
        python -m pytest -c pytest.ini --rootdir=. tests_postgres

The database is wiped (drop_all/create_all) by every test. Never point this
at a real database. Calendar: CARA-native (no external provider at all).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Appointment, AppointmentStatus, BookingProvider, SpaAccount
from app.models.appointment import CardStatus
from app.models.base import Base
from app.services import appointment_booking_service as booking
from app.services.booking_state import arm_verified_proposal
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent

URL = os.environ.get("CARA_TEST_POSTGRES_URL")
if not URL:
    raise RuntimeError("Set CARA_TEST_POSTGRES_URL to a disposable PostgreSQL database")

CHICAGO = ZoneInfo("America/Chicago")


def _slot():
    return (datetime.now(CHICAGO) + timedelta(days=2)).replace(hour=15, minute=0, second=0, microsecond=0)


@pytest_asyncio.fixture
async def pg(monkeypatch):
    engine = create_async_engine(URL, pool_size=10)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    spas = []
    async with maker() as db:
        for name, phone in (("Northside Plumbing", "+15550003000"), ("Elm Street Dental", "+15550004000")):
            spa = SpaAccount(
                id=uuid.uuid4(), name=name, timezone="America/Chicago", twilio_phone_number=phone,
                booking_provider=BookingProvider.GOOGLE_CALENDAR, booking_config={},
                booking_policies={"scheduling_source": "cara_native"},
                business_hours={d: [{"open": "08:00", "close": "18:00"}]
                                for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
                services=[{"name": "Service Visit", "duration_minutes": 60}],
                staff=[{"name": "Technician 1", "services": []}],
            )
            db.add(spa)
            spas.append(spa)
        await db.commit()
    for name in ("_notify_staff_event", "_notify_provider_error", "upsert_external_customer_link"):
        monkeypatch.setattr(booking, name, AsyncMock(return_value=None))
    monkeypatch.setattr(booking, "card_status_for_booked_appointment",
                        AsyncMock(return_value=CardStatus.NOT_REQUIRED))
    # Widen the race window: pause between "is the slot free?" and the write,
    # so concurrent confirmations really interleave. Only the PostgreSQL
    # advisory locks can then keep them from double-booking.
    real_check = booking._has_conflict

    async def slow_check(*args, **kwargs):
        result = await real_check(*args, **kwargs)
        await asyncio.sleep(0.2)
        return result

    monkeypatch.setattr(booking, "_has_conflict", slow_check)
    yield maker, spas
    await engine.dispose()


def _caller(spa, sid, phone):
    return CallSession(sid, "inbound", phone, spa.twilio_phone_number,
                       tenant_id=str(spa.id), timezone=spa.timezone)


async def _staged(maker, spa, sid, phone, start):
    session = _caller(spa, sid, phone)
    async with maker() as db:
        result = await booking.stage_booking(db, session, AppointmentIntent(
            intent="schedule", confidence=1.0, caller_name="Pat Doe",
            requested_start_iso=start.isoformat(), service_description="Service Visit"))
    assert result.outcome is booking.BookingOutcome.DRAFT, result.message
    arm_verified_proposal(session)
    return session


async def _confirm(maker, session):
    async with maker() as db:
        return await booking.confirm_booking(db, session)


async def _live(maker, spa=None):
    async with maker() as db:
        query = select(Appointment).where(
            Appointment.status.in_([AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED]))
        if spa is not None:
            query = query.where(Appointment.tenant_id == spa.id)
        return list((await db.execute(query)).scalars())


async def test_two_callers_racing_for_one_slot_get_one_booking(pg):
    maker, (spa, _other) = pg
    start = _slot()
    a = await _staged(maker, spa, "race-a", "+15550101001", start)
    b = await _staged(maker, spa, "race-b", "+15550101002", start)
    results = await asyncio.gather(_confirm(maker, a), _confirm(maker, b))
    outcomes = sorted(r.outcome.value for r in results)
    assert outcomes == ["booked", "conflict"], [r.message for r in results]
    assert len(await _live(maker, spa)) == 1


async def test_duplicate_confirmations_of_one_call_race_to_one_booking(pg):
    maker, (spa, _other) = pg
    start = _slot()
    session = await _staged(maker, spa, "dup", "+15550101003", start)
    twin = CallSession.from_dict(session.to_dict())  # the same call, retried in parallel
    results = await asyncio.gather(*(_confirm(maker, s) for s in (session, twin)))
    assert all(r.outcome in {booking.BookingOutcome.BOOKED, booking.BookingOutcome.RESCHEDULED}
               for r in results), [r.message for r in results]
    assert len(await _live(maker, spa)) == 1


async def test_same_time_at_two_businesses_is_two_independent_bookings(pg):
    maker, (first, second) = pg
    start = _slot()
    a = await _staged(maker, first, "tenant-a", "+15550101004", start)
    b = await _staged(maker, second, "tenant-b", "+15550101004", start)  # same caller number
    results = await asyncio.gather(_confirm(maker, a), _confirm(maker, b))
    assert [r.outcome for r in results] == [booking.BookingOutcome.BOOKED] * 2
    assert len(await _live(maker, first)) == len(await _live(maker, second)) == 1
    async with maker() as db:
        assert [r.id for r in await booking.lookup_upcoming_appointments(db, a)] == [results[0].appointment.id]
        assert [r.id for r in await booking.lookup_upcoming_appointments(db, b)] == [results[1].appointment.id]


async def test_many_callers_racing_never_overbook(pg):
    maker, (spa, _other) = pg
    start = _slot()
    sessions = [await _staged(maker, spa, f"crowd-{n}", f"+155501020{n:02d}", start) for n in range(6)]
    results = await asyncio.gather(*(_confirm(maker, s) for s in sessions))
    assert sum(r.outcome is booking.BookingOutcome.BOOKED for r in results) == 1
    assert len(await _live(maker, spa)) == 1
