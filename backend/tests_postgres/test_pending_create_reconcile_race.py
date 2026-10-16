"""Disposable PostgreSQL only (G2): concurrent pending-create work stays single.

* Two reconciliations of the same unresolved Google create, at the same time,
  link exactly ONE local appointment.
* Two attempts to open a pending create for the same booking intent, at the
  same time: exactly one succeeds, the other is refused (CreateInFlight).

SIMULATED Google transport only.
"""
import asyncio
import uuid
from datetime import timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models import Appointment, Contact
from app.models.google_calendar_connection import GoogleCalendarConnection
from app.services import booking_escalations as esc
from app.services import google_calendar as gc
from app.services.google_create_reconciliation import reconcile_pending_create
from tests.test_brick1_google_create_recovery import CALENDAR, RecoveringGoogle
from tests_postgres.test_postgres_booking_races import _slot, pg  # noqa: F401  (fixture)


async def _setup(maker, spa, monkeypatch, key):
    fake = RecoveringGoogle("ok")
    real_get = fake._get

    def slow_get(kw):
        import time
        time.sleep(0.3)   # runs in a worker thread: widens the reconcile race window
        return real_get(kw)

    fake._get = slow_get

    async def _service(_conn):
        return fake.service()

    monkeypatch.setattr(gc, "_service", _service)
    start = _slot()
    end = start + timedelta(hours=1)
    async with maker() as db:
        contact = Contact(id=uuid.uuid4(), tenant_id=spa.id, phone_number="+15550009001")
        connection = GoogleCalendarConnection(spa_id=spa.id, selected_calendar_id=CALENDAR,
                                              credentials={}, scopes=[], status="connected")
        db.add_all([contact, connection])
        await db.commit()
        event_id = await gc.create_event(connection, title="Visit", start=start, end=end,
                                         timezone_name="America/Chicago", description="", event_id=key)
        details = {"booking_reference": key, "provider": "google_calendar", "calendar_id": CALENDAR,
                   "event_id": event_id, "requested_start": start.astimezone(timezone.utc).isoformat(),
                   "requested_end": end.astimezone(timezone.utc).isoformat(),
                   "customer_phone": contact.phone_number, "title": "Visit", "contact_id": contact.id}
        record_id = await esc.open_pending_create(db, spa.id, key, details)
        await esc.mark_pending_create_unresolved(db, record_id, {"error_code": "BOOKING_OUTCOME_UNKNOWN"},
                                                 escalated=True)
    return fake, record_id, event_id


async def test_concurrent_reconciliations_link_exactly_one_appointment(pg, monkeypatch):
    maker, spas = pg
    spa = spas[0]
    key = f"CA-pg-reconcile:{uuid.uuid4()}"
    fake, record_id, event_id = await _setup(maker, spa, monkeypatch, key)

    async def reconcile():
        async with maker() as db:
            return (await reconcile_pending_create(db, spa.id, record_id, uuid.uuid4()))["status"]

    results = sorted(await asyncio.gather(reconcile(), reconcile()))
    async with maker() as db:
        rows = list((await db.execute(select(Appointment).where(
            Appointment.tenant_id == spa.id, Appointment.booking_intent_key == key))).scalars())
    assert len(rows) == 1, (results, rows)
    assert rows[0].external_booking_id == event_id
    assert "booked" in results and set(results) <= {"booked", "already_resolved", "already_linked"}, results
    assert len(fake.inserted_ids) == 1                    # reconciliation never inserted


async def test_concurrent_opens_for_one_intent_allow_exactly_one(pg):
    maker, spas = pg
    spa = spas[1]
    key = f"CA-pg-open:{uuid.uuid4()}"

    async def open_once():
        async with maker() as db:
            try:
                await esc.open_pending_create(db, spa.id, key, {"booking_reference": key})
                return "opened"
            except esc.CreateInFlight:
                return "refused"

    results = sorted(await asyncio.gather(*(open_once() for _ in range(4))))
    assert results == ["opened", "refused", "refused", "refused"], results
    async with maker() as db:
        count = (await db.execute(select(func.count()).select_from(esc.BookingEscalation).where(
            esc.BookingEscalation.tenant_id == spa.id,
            esc.BookingEscalation.category == esc.PENDING_CREATE))).scalar_one()
    assert count == 1
