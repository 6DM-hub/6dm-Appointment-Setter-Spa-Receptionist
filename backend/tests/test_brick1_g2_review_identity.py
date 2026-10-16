"""Codex review P1 (SQLite part): reconciliation recovers the caller's identity
only from durable, tenant-scoped data. The real-transaction reproduction (the
rolled-back brand-new contact and the PostgreSQL foreign key) is
tests_postgres/test_pending_create_real_boundaries.py.

SIMULATED Google transport only.
"""
import uuid
from datetime import timezone

from sqlalchemy import select

from app.models import Appointment, Contact, SpaAccount
from app.models.google_calendar_connection import GoogleCalendarConnection
from app.services import booking_escalations as esc
from app.services import google_calendar as gc
from app.services.google_create_reconciliation import reconcile_pending_create
from tests.test_brick1_google_create_recovery import CALENDAR, END, KEY, START, RecoveringGoogle
from tests.test_cara_native_double_booking import rubyz_db  # noqa: F401  (fixture)

PHONE = "+15550203333"


async def _event(db, spa, monkeypatch):
    fake = RecoveringGoogle("ok")
    connection = GoogleCalendarConnection(spa_id=spa.id, selected_calendar_id=CALENDAR,
                                          credentials={}, scopes=[], status="connected")
    db.add(connection)
    await db.commit()

    async def _service(_conn):
        return fake.service()

    monkeypatch.setattr(gc, "_service", _service)
    event_id = await gc.create_event(connection, title="Visit", start=START, end=END,
                                     timezone_name="America/Chicago", description="", event_id=KEY)
    return fake, event_id


def _details(event_id, contact_id):
    return {"booking_reference": KEY, "provider": "google_calendar", "calendar_id": CALENDAR,
            "event_id": event_id, "requested_start": START.astimezone(timezone.utc).isoformat(),
            "requested_end": END.astimezone(timezone.utc).isoformat(), "customer_phone": PHONE,
            "contact_id": contact_id, "contact_phone": PHONE, "contact_first_name": "Ada",
            "contact_last_name": "Lovelace", "contact_email": None, "title": "Visit"}


async def _reconcile(db, spa_id, details):
    record_id = await esc.open_pending_create(db, spa_id, KEY, details)
    await esc.mark_pending_create_unresolved(db, record_id, {}, escalated=True)
    return await reconcile_pending_create(db, spa_id, record_id, uuid.uuid4())


async def _contacts(db, spa_id):
    return list((await db.execute(select(Contact).where(Contact.tenant_id == spa_id))).scalars())


async def test_a_dangling_contact_id_is_rebuilt_from_the_recorded_identity(rubyz_db, monkeypatch):
    db, spa = rubyz_db
    spa_id = spa.id
    _fake, event_id = await _event(db, spa, monkeypatch)
    result = await _reconcile(db, spa_id, _details(event_id, uuid.uuid4()))   # contact never committed
    assert result["status"] == "booked", result
    [contact] = await _contacts(db, spa_id)
    assert (contact.phone_number, contact.first_name, contact.last_name) == (PHONE, "Ada", "Lovelace")
    [row] = list((await db.execute(select(Appointment).where(Appointment.tenant_id == spa_id))).scalars())
    assert row.contact_id == contact.id


async def test_an_existing_contact_for_the_phone_is_reused_not_duplicated(rubyz_db, monkeypatch):
    db, spa = rubyz_db
    spa_id = spa.id
    _fake, event_id = await _event(db, spa, monkeypatch)
    existing = Contact(id=uuid.uuid4(), tenant_id=spa_id, phone_number=PHONE, first_name="Ada")
    db.add(existing)
    await db.commit()
    existing_id = existing.id
    result = await _reconcile(db, spa_id, _details(event_id, uuid.uuid4()))
    assert result["status"] == "booked", result
    contacts = await _contacts(db, spa_id)
    assert [c.id for c in contacts] == [existing_id]


async def test_another_tenants_contact_is_never_used(rubyz_db, monkeypatch):
    db, spa = rubyz_db
    spa_id = spa.id
    other = SpaAccount(id=uuid.uuid4(), name="Elsewhere", timezone="America/Chicago",
                       twilio_phone_number="+15550009999", booking_config={}, booking_policies={},
                       business_hours={}, services=[], staff=[])
    db.add(other)
    await db.commit()
    foreign = Contact(id=uuid.uuid4(), tenant_id=other.id, phone_number=PHONE, first_name="Someone")
    db.add(foreign)
    await db.commit()
    foreign_id = foreign.id
    _fake, event_id = await _event(db, spa, monkeypatch)
    result = await _reconcile(db, spa_id, _details(event_id, foreign_id))   # id of ANOTHER tenant's contact
    assert result["status"] == "booked", result
    [contact] = await _contacts(db, spa_id)
    assert contact.id != foreign_id and contact.tenant_id == spa_id
    [row] = list((await db.execute(select(Appointment).where(Appointment.tenant_id == spa_id))).scalars())
    assert row.contact_id == contact.id
