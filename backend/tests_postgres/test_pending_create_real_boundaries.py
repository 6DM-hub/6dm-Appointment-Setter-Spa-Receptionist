"""Disposable PostgreSQL only: G2 recovery across REAL transaction boundaries.

Codex review (2026-10-16), P1: a brand-new caller's Contact is only flushed
inside the booking transaction. The pending-create record is committed on its
own connection before the Google write. When the create outcome is unclear
the booking transaction rolls back and the new Contact disappears; the record
survives with a dangling contact id. Reconciliation (here in a NEW session,
as after a restart) must still link exactly one valid, tenant-scoped contact
and appointment. Reproduced on b7a36d0: ForeignKeyViolationError.

SIMULATED Google transport only; the database, transactions and foreign keys
are real.
"""
import uuid

from sqlalchemy import func, select

from app.models import Appointment, Contact
from app.models.booking_escalation import BookingEscalation
from app.models.google_calendar_connection import GoogleCalendarConnection
from app.services import appointment_booking_service as booking
from app.services import booking_escalations as esc
from app.services import google_calendar as gc
from app.services.booking_adapters.google_calendar import GoogleCalendarAdapter
from app.services.google_create_reconciliation import reconcile_pending_create
from tests.test_brick1_google_create_recovery import CALENDAR, RecoveringGoogle
from tests_postgres.test_postgres_booking_races import _confirm, _slot, _staged, pg  # noqa: F401

PHONE = "+15550107777"


async def _google_tenant(maker, spa, monkeypatch, fake):
    async with maker() as db:
        connection = GoogleCalendarConnection(spa_id=spa.id, selected_calendar_id=CALENDAR,
                                              credentials={}, scopes=[], status="connected")
        db.add(connection)
        await db.commit()
    adapter = GoogleCalendarAdapter(calendar_id=CALENDAR, connection=connection,
                                    timezone_name=spa.timezone)
    monkeypatch.setattr(booking, "get_booking_adapter", lambda **_k: adapter)

    async def _service(_conn):
        return fake.service()

    monkeypatch.setattr(gc, "_service", _service)


async def _count(maker, model, *where):
    async with maker() as db:
        return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def _lost_create(maker, spa, monkeypatch, sid):
    """A brand-new caller confirms; Google stores the event, the reply is lost
    and the exact-id lookup fails, so the outcome is unknown."""
    fake = RecoveringGoogle("lost_reply", get_fails="transport")
    await _google_tenant(maker, spa, monkeypatch, fake)
    start = _slot()
    assert await _count(maker, Contact, Contact.tenant_id == spa.id, Contact.phone_number == PHONE) == 0
    session = await _staged(maker, spa, sid, PHONE, start)
    result = await _confirm(maker, session)
    assert result.outcome is booking.BookingOutcome.ERROR, result.message
    assert len(fake.state) == 1 and len(fake.inserted_ids) == 1   # Google holds the event
    async with maker() as db:
        rows = list((await db.execute(select(BookingEscalation).where(
            BookingEscalation.tenant_id == spa.id))).scalars())
    pending = [r for r in rows if r.category == esc.PENDING_CREATE]
    assert len(pending) == 1 and pending[0].status == "pending", rows
    fake.get_fails = None                                         # Google is readable again
    return fake, start, pending[0], rows


async def test_new_caller_lost_create_reconciles_to_one_valid_contact_and_appointment(pg, monkeypatch):
    maker, (spa, _other) = pg
    fake, _start, pending, _rows = await _lost_create(maker, spa, monkeypatch, "CA-new-caller-lost")
    # The rollback removed the brand-new contact the record points at.
    assert await _count(maker, Contact, Contact.tenant_id == spa.id) == 0
    async with maker() as db:   # a NEW session, as after a restart
        outcome = await reconcile_pending_create(db, spa.id, pending.id, uuid.uuid4())
    assert outcome["status"] == "booked", outcome
    async with maker() as db:
        contacts = list((await db.execute(select(Contact).where(
            Contact.tenant_id == spa.id, Contact.phone_number == PHONE))).scalars())
        appointments = list((await db.execute(select(Appointment).where(
            Appointment.tenant_id == spa.id))).scalars())
    assert len(contacts) == 1, contacts
    assert contacts[0].first_name == "Pat" and contacts[0].last_name == "Doe"
    assert len(appointments) == 1, appointments
    assert appointments[0].contact_id == contacts[0].id
    assert appointments[0].external_booking_id in fake.state
    assert len(fake.inserted_ids) == 1                            # no second provider insert
    assert await _count(maker, Contact, Contact.tenant_id == _other.id) == 0   # tenant-scoped
