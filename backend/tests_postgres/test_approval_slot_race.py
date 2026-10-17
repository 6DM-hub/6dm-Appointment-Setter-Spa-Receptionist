"""Real PostgreSQL locking; Square responses are simulated, never live."""
import asyncio

from sqlalchemy import select

from app.models import Appointment, AppointmentStatus
from app.services import appointment_booking_service as booking
from app.services.booking_state import arm_verified_proposal
from tests.test_brick1_approval_slot_safety import install_pending_square
from tests.test_codex_brick1_tenant_boundaries import setup_tenants, session_for, stage
from tests_postgres.test_postgres_booking_races import pg  # noqa: F401


async def test_two_square_callers_race_for_one_pending_approval_slot(pg, monkeypatch):
    maker, spas = pg
    async with maker() as db:
        _, spa, _, squares = await setup_tenants((db, spas[0]), monkeypatch)
    square = squares["fake-cleaning-token"]
    install_pending_square(monkeypatch, square)
    callers = [session_for(spa, f"approval-race-{n}", phone=f"+1555000787{n}") for n in (1, 2)]
    for caller in callers:
        async with maker() as db:
            await stage(db, spa, caller)
        arm_verified_proposal(caller)

    ready = 0
    gate = asyncio.Event()

    async def confirm(caller):
        nonlocal ready
        async with maker() as db:
            ready += 1
            if ready == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), timeout=5)
            return await booking.confirm_booking(db, caller)

    results = await asyncio.gather(*(confirm(caller) for caller in callers))
    assert len(square.bookings) == 1, "TWO SQUARE BOOKINGS IN PENDING SLOT"
    assert sorted(result.outcome.value for result in results) == ["awaiting_provider_approval", "conflict"]
    async with maker() as db:
        appointments = list((await db.execute(select(Appointment).where(Appointment.tenant_id == spa.id))).scalars())
        assert len(appointments) == 1
        assert appointments[0].status == AppointmentStatus.AWAITING_PROVIDER_APPROVAL
        assert appointments[0].external_booking_id == next(iter(square.bookings))
