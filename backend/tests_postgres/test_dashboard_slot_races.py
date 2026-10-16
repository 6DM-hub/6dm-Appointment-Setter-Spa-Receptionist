"""Disposable PostgreSQL only: dashboard and voice share the calendar lock."""
import asyncio
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from app.api.v1.appointments import update_appointment
from app.core.tenancy import TenantScope
from app.models import Appointment, AppointmentStatus
from app.schemas import AppointmentUpdate
from app.services import appointment_booking_service as booking
from tests_postgres.test_postgres_booking_races import pg, _slot, _staged


async def test_dashboard_move_racing_voice_confirm_cannot_double_book(pg):
    maker, spas = pg
    spa = spas[0]
    target = _slot()
    original = await _staged(maker, spa, "CA-dashboard-original", "+15550008001", target - timedelta(hours=2))
    async with maker() as db:
        booked = await booking.confirm_booking(db, original)
        assert booked.outcome is booking.BookingOutcome.BOOKED
        row_id = booked.appointment.id
    caller = await _staged(maker, spa, "CA-dashboard-racer", "+15550008002", target)

    async def dashboard():
        async with maker() as db:
            try:
                await update_appointment(row_id, AppointmentUpdate(start_time=target, end_time=target + timedelta(hours=1)),
                                         scope=TenantScope.for_tenant(spa.id), db=db)
                return "moved"
            except HTTPException as exc:
                assert exc.status_code == 409
                return "conflict"

    async def voice():
        async with maker() as db:
            return (await booking.confirm_booking(db, caller)).outcome.value

    results = await asyncio.gather(dashboard(), voice())
    assert results.count("conflict") == 1, results
    async with maker() as db:
        occupied = list((await db.execute(select(Appointment).where(
            Appointment.tenant_id == spa.id,
            Appointment.status.in_([AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED]),
            Appointment.start_time < target + timedelta(hours=1), Appointment.end_time > target,
        ))).scalars())
        assert len(occupied) == 1, results
