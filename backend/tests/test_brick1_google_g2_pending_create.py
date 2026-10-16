"""G2: a Google create survives a lost reply or a restart.

* Before Google is asked, the attempt is durably recorded (pending create).
* A verified create removes the record with the local appointment; a definite
  refusal removes it; an unclear outcome keeps it (linked to the staff
  escalation); a dead worker leaves it held and the dispatcher alerts staff.
* While open it blocks another attempt of the same intent, and a new call for
  the same caller and time.
* Explicit reconciliation only READS Google and links the verified event to
  ONE local appointment.

SIMULATED Google only. Engine tests use the in-memory FakeDB world; store and
reconciliation tests use a real SQLite database. The PostgreSQL concurrency
case is tests_postgres/test_pending_create_reconcile_race.py.
"""
from __future__ import annotations

import uuid
from datetime import timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update

from app.models import Appointment, Contact
from app.models.booking_escalation import BookingEscalation
from app.models.google_calendar_connection import GoogleCalendarConnection
from app.services import appointment_booking_service as booking
from app.services import booking_escalations as esc
from app.services import google_calendar as gc
from app.services.booking_state import arm_verified_proposal, get_draft, intent_key
from app.services.google_create_reconciliation import reconcile_pending_create
from tests.square_world import local
from tests.test_brick1_booking_change_guard import Phone, T2
from tests.test_brick1_google_create_recovery import (
    CALENDAR, END, KEY, START, RecoveringGoogle, _shift_start, _voice_world,
)
from tests.test_cara_native_double_booking import rubyz_db  # noqa: F401  (fixture)


# --------------------------------------------------------------------------- #
# Engine (FakeDB world)
# --------------------------------------------------------------------------- #
def _pending(w):
    return [row for row in w.unresolved if row.get("category") == esc.PENDING_CREATE]


async def _staged_phone(w):
    p = Phone(w)
    p.voice._pending_caller = f"I'd like a {w.first} on Thursday at 2 PM. My name is Ada Lovelace."
    p.voice._flush_caller_turn()
    out = await p.propose(requested_start_iso=local(T2), service_description=w.first,
                          caller_name="Ada Lovelace", operation="schedule")
    assert out["status"] == "draft", out
    return p


async def test_attempt_is_recorded_before_google_is_asked_and_removed_on_success(monkeypatch):
    fake = RecoveringGoogle("ok")
    w = _voice_world(monkeypatch, "hvac", fake)
    seen_at_insert = []
    original = fake._insert

    def watching(kw):
        seen_at_insert.append([dict(row) for row in _pending(w)])
        return original(kw)

    fake._insert = watching
    p = await _staged_phone(w)
    await p.says("Yes, please.")
    assert len(seen_at_insert) == 1 and len(seen_at_insert[0]) == 1
    record = seen_at_insert[0][0]
    assert record["stage"] == "in_flight" and record["delivery_status"] == "held"
    assert record["event_id"] == gc.deterministic_event_id(record["booking_reference"])
    assert record["calendar_id"] == CALENDAR
    assert _pending(w) == []                              # removed with the local row
    assert len(list(w.store.live)) == 1


async def test_definite_refusal_removes_the_record(monkeypatch):
    w = _voice_world(monkeypatch, "spa", RecoveringGoogle("http_403"))
    p = await _staged_phone(w)
    await p.says("Yes, please.")
    assert _pending(w) == []
    assert list(w.store.live) == []


async def test_unclear_outcome_keeps_the_record_linked_to_the_escalation(monkeypatch):
    w = _voice_world(monkeypatch, "dental", RecoveringGoogle("lost_reply", tamper=_shift_start))
    p = await _staged_phone(w)
    await p.says("Yes, please.")
    [record] = _pending(w)
    assert record["stage"] == "unknown_outcome"
    assert record["delivery_status"] == "linked_to_escalation"
    assert record["error_code"] == "BOOKING_OUTCOME_UNKNOWN"
    assert len(w.escalations) == 1 and list(w.store.live) == []


async def test_open_attempt_of_the_same_intent_blocks_a_second_send(monkeypatch):
    fake = RecoveringGoogle("ok")
    w = _voice_world(monkeypatch, "spa", fake)
    p = await _staged_phone(w)
    session = p.session
    key = intent_key(session, get_draft(session))
    # An earlier worker recorded this intent and died before settling it.
    w.unresolved.append({"category": esc.PENDING_CREATE, "record_id": str(uuid.uuid4()),
                         "booking_reference": key, "stage": "in_flight", "delivery_status": "held"})
    arm_verified_proposal(session)
    result = await booking.confirm_booking(w.db, session)
    assert result.outcome is booking.BookingOutcome.ERROR
    assert "still needs staff verification" in result.message
    assert fake.inserted_ids == []                        # nothing sent
    assert list(w.store.live) == []


async def test_record_that_cannot_be_written_means_nothing_is_sent(monkeypatch):
    fake = RecoveringGoogle("ok")
    w = _voice_world(monkeypatch, "hvac", fake)

    async def broken(*_a, **_k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(booking, "_open_pending_create", broken)
    p = await _staged_phone(w)
    arm_verified_proposal(p.session)
    result = await booking.confirm_booking(w.db, p.session)
    assert result.outcome is booking.BookingOutcome.ERROR
    assert "nothing was booked" in result.message
    assert fake.inserted_ids == [] and list(w.store.live) == []


# --------------------------------------------------------------------------- #
# Store and reconciliation (real SQLite)
# --------------------------------------------------------------------------- #
async def _world(db, spa, monkeypatch, fake, *, tamper=None):
    contact = Contact(id=uuid.uuid4(), tenant_id=spa.id, phone_number="+15550002222")
    connection = GoogleCalendarConnection(spa_id=spa.id, selected_calendar_id=CALENDAR,
                                          credentials={}, scopes=[], status="connected")
    db.add_all([contact, connection])
    await db.commit()

    async def _service(_conn):
        return fake.service()

    monkeypatch.setattr(gc, "_service", _service)
    event_id = await gc.create_event(connection, title="Visit", start=START, end=END,
                                     timezone_name="America/Chicago", description="", event_id=KEY)
    if tamper:
        tamper(fake.state[event_id])
    return contact, connection, event_id


def _details(contact, event_id):
    return {"booking_reference": KEY, "provider": "google_calendar", "calendar_id": CALENDAR,
            "event_id": event_id, "requested_start": START.astimezone(timezone.utc).isoformat(),
            "requested_end": END.astimezone(timezone.utc).isoformat(),
            "customer_phone": contact.phone_number, "title": "Visit", "description": "Booked by CARA",
            "contact_id": contact.id, "call_sid": "CA-test"}


async def _open_unresolved(db, spa, details):
    record_id = await esc.open_pending_create(db, spa.id, KEY, details)
    await esc.mark_pending_create_unresolved(db, record_id, {"error_code": "BOOKING_OUTCOME_UNKNOWN"},
                                             escalated=True)
    return record_id


async def _appointments(db, spa_id):
    return (await db.execute(select(func.count()).select_from(Appointment).where(
        Appointment.tenant_id == spa_id))).scalar_one()


async def test_store_is_exclusive_per_intent_and_blocks_a_new_call(rubyz_db):
    db, spa = rubyz_db
    details = {"booking_reference": KEY, "customer_phone": "+15550002222",
               "requested_start": START.astimezone(timezone.utc).isoformat()}
    record_id = await esc.open_pending_create(db, spa.id, KEY, details)
    with pytest.raises(esc.CreateInFlight):
        await esc.open_pending_create(db, spa.id, KEY, details)
    row = await db.get(BookingEscalation, record_id)
    assert (row.category, row.status, row.delivery_status) == (esc.PENDING_CREATE, "pending", "held")
    assert row.details["stage"] == "in_flight"
    blocking = await esc.unresolved_booking(db, spa.id, "+15550002222", START)
    assert blocking is not None and blocking.id == record_id
    assert await esc.unresolved_booking(db, uuid.uuid4(), "+15550002222", START) is None


async def test_stale_held_record_is_promoted_to_a_staff_alert(rubyz_db):
    db, spa = rubyz_db
    record_id = await esc.open_pending_create(db, spa.id, KEY, {"booking_reference": KEY})
    await db.execute(update(BookingEscalation).where(BookingEscalation.id == record_id).values(
        created_at=esc.now() - timedelta(minutes=10)))
    await db.commit()
    assert await esc.promote_stale_change_fences(db)
    row = await db.get(BookingEscalation, record_id)
    await db.refresh(row)
    assert row.delivery_status == "pending" and row.details["stage"] == "interrupted"


async def test_held_record_cannot_be_resolved_while_in_flight(rubyz_db):
    db, spa = rubyz_db
    record_id = await esc.open_pending_create(db, spa.id, KEY, {"booking_reference": KEY})
    with pytest.raises(HTTPException) as exc:
        await esc.resolve(db, spa.id, record_id, uuid.uuid4(), "unable_to_book", 1, "")
    assert exc.value.status_code == 409


async def test_reconcile_links_the_verified_event_to_one_appointment_read_only(rubyz_db, monkeypatch):
    db, spa = rubyz_db
    spa_id = spa.id
    fake = RecoveringGoogle("ok")
    contact, _, event_id = await _world(db, spa, monkeypatch, fake)
    record_id = await _open_unresolved(db, spa, _details(contact, event_id))
    inserts_before = list(fake.inserted_ids)
    result = await reconcile_pending_create(db, spa_id, record_id, uuid.uuid4())
    assert result["status"] == "booked" and result["event_id"] == event_id
    assert fake.inserted_ids == inserts_before            # read-only: no create
    assert fake.gets == [event_id]                        # one exact read
    rows = list((await db.execute(select(Appointment).where(Appointment.tenant_id == spa_id))).scalars())
    assert len(rows) == 1
    assert rows[0].external_booking_id == event_id and rows[0].booking_intent_key == KEY
    row = await db.get(BookingEscalation, record_id)
    await db.refresh(row)
    assert row.status == "booked" and row.details["reconciled_appointment_id"] == str(rows[0].id)
    again = await reconcile_pending_create(db, spa_id, record_id, uuid.uuid4())
    assert again["status"] == "already_resolved"
    assert await _appointments(db, spa_id) == 1


async def test_reconcile_reuses_an_already_linked_row(rubyz_db, monkeypatch):
    db, spa = rubyz_db
    spa_id = spa.id
    fake = RecoveringGoogle("ok")
    contact, _, event_id = await _world(db, spa, monkeypatch, fake)
    record_id = await _open_unresolved(db, spa, _details(contact, event_id))
    db.add(Appointment(tenant_id=spa.id, contact_id=contact.id, title="Visit", start_time=START,
                       end_time=END, booking_provider="google_calendar", external_booking_id=event_id,
                       booking_intent_key=KEY))
    await db.commit()
    result = await reconcile_pending_create(db, spa_id, record_id, uuid.uuid4())
    assert result["status"] == "booked"
    assert await _appointments(db, spa_id) == 1


@pytest.mark.parametrize("problem", ["different_time", "calendar_changed", "event_missing"])
async def test_reconcile_without_proof_is_unknown_and_creates_nothing(rubyz_db, monkeypatch, problem):
    db, spa = rubyz_db
    spa_id = spa.id
    fake = RecoveringGoogle("ok")
    contact, connection, event_id = await _world(
        db, spa, monkeypatch, fake, tamper=_shift_start if problem == "different_time" else None)
    if problem == "calendar_changed":
        connection.selected_calendar_id = "another-calendar"
        await db.commit()
    if problem == "event_missing":
        fake.state.clear()
    record_id = await _open_unresolved(db, spa, _details(contact, event_id))
    inserts_before = list(fake.inserted_ids)
    result = await reconcile_pending_create(db, spa_id, record_id, uuid.uuid4())
    assert result["status"] == "unknown"
    assert await _appointments(db, spa_id) == 0
    assert fake.inserted_ids == inserts_before
    row = await db.get(BookingEscalation, record_id)
    await db.refresh(row)
    assert row.status == "pending"                        # still blocked, still staff work


async def test_reconcile_refuses_a_fresh_in_flight_record_and_other_tenants(rubyz_db, monkeypatch):
    db, spa = rubyz_db
    spa_id = spa.id
    fake = RecoveringGoogle("ok")
    contact, _, event_id = await _world(db, spa, monkeypatch, fake)
    record_id = await esc.open_pending_create(db, spa.id, KEY, _details(contact, event_id))
    with pytest.raises(HTTPException) as busy:
        await reconcile_pending_create(db, spa_id, record_id, uuid.uuid4())
    assert busy.value.status_code == 409
    with pytest.raises(HTTPException) as other:
        await reconcile_pending_create(db, uuid.uuid4(), record_id, uuid.uuid4())
    assert other.value.status_code == 404
    assert await _appointments(db, spa_id) == 0
