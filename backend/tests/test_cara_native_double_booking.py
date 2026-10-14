"""CARA-native bookings against a real (SQLite) database: one slot, many callers.

Both callers are told the time is open, then race to confirm. The confirm
path re-checks CARA's own appointments against staff capacity before writing,
so the slower caller must be refused, never double-booked. (Production also
serializes this re-check with a per-tenant PostgreSQL advisory lock, which
SQLite cannot exercise; this test covers the re-check itself.)
"""
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Appointment, AppointmentStatus, BookingProvider, SpaAccount
from app.models.appointment import CardStatus
from app.models.base import Base
from app.services import appointment_booking_service as booking
from app.services.booking_state import arm_verified_proposal
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent
import tests.test_cara_manager  # noqa: F401  (registers the SQLite JSONB shim)

CHICAGO = ZoneInfo("America/Chicago")


def _slot_start():
    """A future 4 PM Chicago time, two days out, inside the spa's hours."""
    local = (booking.datetime.now(CHICAGO) + timedelta(days=2)).replace(
        hour=16, minute=0, second=0, microsecond=0
    )
    return local


def _db_rows(start):
    return select(Appointment).where(
        Appointment.status.in_([AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED])
    )


@pytest_asyncio.fixture
async def rubyz_db(request, monkeypatch):
    staff_count = getattr(request, "param", 1)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def functions(connection, _record):
        connection.create_function("num_nonnulls", -1, lambda *args: sum(x is not None for x in args))
        connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        spa = SpaAccount(
            id=uuid.uuid4(),
            name="Rubyz",
            timezone="America/Chicago",
            twilio_phone_number="+15550001000",
            booking_provider=BookingProvider.GOOGLE_CALENDAR,
            booking_config={},
            booking_policies={"scheduling_source": "cara_native"},
            business_hours={d: [{"open": "09:00", "close": "18:00"}]
                            for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
            services=[{"name": "Signature Facial", "duration_minutes": 60}],
            staff=[{"name": f"Therapist {i}", "services": []} for i in range(staff_count)],
        )
        db.add(spa)
        await db.commit()
        monkeypatch.setattr(booking, "upsert_external_customer_link", AsyncMock(return_value=None))
        monkeypatch.setattr(booking, "card_status_for_booked_appointment",
                            AsyncMock(return_value=CardStatus.NOT_REQUIRED))
        monkeypatch.setattr(booking, "_notify_staff_event", AsyncMock())
        monkeypatch.setattr(booking, "_notify_provider_error", AsyncMock())
        yield db, spa
    await engine.dispose()


def _caller(spa, n):
    return CallSession(f"cara-native-{n}", "inbound", f"+1999555010{n}", spa.twilio_phone_number,
                       tenant_id=str(spa.id), timezone=spa.timezone)


def _request(start, n):
    return AppointmentIntent(intent="schedule", caller_name=f"Test Caller {n}",
                             requested_start_iso=start.isoformat(),
                             service_description="Signature Facial")


async def _stage(db, spa, start, n):
    session = _caller(spa, n)
    staged = await booking.stage_booking(db, session, _request(start, n))
    return session, staged


@pytest.mark.asyncio
async def test_two_callers_one_therapist_only_one_booking(rubyz_db):
    db, spa = rubyz_db
    start = _slot_start()

    # Both callers hear the slot is open before either confirms.
    a, staged_a = await _stage(db, spa, start, 1)
    b, staged_b = await _stage(db, spa, start, 2)
    assert staged_a.outcome is booking.BookingOutcome.DRAFT, staged_a.message
    assert staged_b.outcome is booking.BookingOutcome.DRAFT, staged_b.message

    arm_verified_proposal(a)
    booked = await booking.confirm_booking(db, a)
    assert booked.outcome is booking.BookingOutcome.BOOKED, booked.message

    arm_verified_proposal(b)
    late = await booking.confirm_booking(db, b)
    assert late.outcome is booking.BookingOutcome.CONFLICT, late.message

    rows = list((await db.execute(_db_rows(start))).scalars())
    assert len(rows) == 1
    assert rows[0].booking_provider == "cara_native"
    assert rows[0].external_booking_id.startswith("cara:cara-native-1:")
    saved = rows[0].start_time
    saved = saved.replace(tzinfo=booking.timezone.utc) if saved.tzinfo is None else saved
    assert saved == start.astimezone(booking.timezone.utc)


@pytest.mark.asyncio
async def test_after_booking_the_exact_check_no_longer_offers_that_time(rubyz_db):
    db, spa = rubyz_db
    start = _slot_start()
    a, _ = await _stage(db, spa, start, 1)
    arm_verified_proposal(a)
    assert (await booking.confirm_booking(db, a)).outcome is booking.BookingOutcome.BOOKED

    check = AppointmentIntent(intent="inquiry", confidence=1.0,
                              requested_start_iso=start.replace(tzinfo=None).isoformat(),
                              service_description="Signature Facial")
    result = await booking.check_availability_only(db, _caller(spa, 3), check)
    assert result.available is False


@pytest.mark.asyncio
@pytest.mark.parametrize("rubyz_db", [2], indirect=True)
async def test_two_therapists_allow_two_bookings_and_refuse_a_third(rubyz_db):
    db, spa = rubyz_db
    start = _slot_start()
    sessions = []
    for n in (1, 2, 3):
        session, staged = await _stage(db, spa, start, n)
        assert staged.outcome is booking.BookingOutcome.DRAFT, staged.message
        sessions.append(session)

    outcomes = []
    for session in sessions:
        arm_verified_proposal(session)
        outcomes.append((await booking.confirm_booking(db, session)).outcome)

    assert outcomes == [booking.BookingOutcome.BOOKED, booking.BookingOutcome.BOOKED,
                        booking.BookingOutcome.CONFLICT]
    assert len(list((await db.execute(_db_rows(start))).scalars())) == 2


@pytest.mark.asyncio
async def test_same_caller_confirming_twice_is_one_booking(rubyz_db):
    db, spa = rubyz_db
    start = _slot_start()
    a, _ = await _stage(db, spa, start, 1)
    arm_verified_proposal(a)
    first = await booking.confirm_booking(db, a)
    arm_verified_proposal(a)
    second = await booking.confirm_booking(db, a)
    assert first.outcome is booking.BookingOutcome.BOOKED
    # The replay reuses the existing row (reported via the existing
    # same-intent path); it is never a clash and never a second booking.
    assert second.outcome in {booking.BookingOutcome.BOOKED, booking.BookingOutcome.RESCHEDULED}
    assert len(list((await db.execute(_db_rows(start))).scalars())) == 1
