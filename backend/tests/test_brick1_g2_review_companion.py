"""Codex review P2 (SQLite and engine part): an unclear Google create leaves
two work items - the pending-create record and the ordinary per-call booking
escalation. They are explicitly linked, and a successful reconciliation
settles BOTH atomically, with history; unrelated work items are never
touched. The real-transaction flow is in
tests_postgres/test_pending_create_real_boundaries.py.

SIMULATED Google transport only.
"""
import uuid

from sqlalchemy import select

from app.models import SpaAccount
from app.models.booking_escalation import BookingEscalation
from app.services import booking_escalations as esc
from app.services.google_create_reconciliation import reconcile_pending_create
from tests.test_brick1_g2_review_identity import PHONE, _details, _event
from tests.test_brick1_google_create_recovery import KEY, START, RecoveringGoogle, _shift_start, _voice_world
from tests.test_brick1_google_g2_pending_create import _pending, _staged_phone
from tests.test_cara_native_double_booking import rubyz_db  # noqa: F401  (fixture)


async def test_unclear_create_links_the_record_to_its_escalation(monkeypatch):
    w = _voice_world(monkeypatch, "spa", RecoveringGoogle("lost_reply", tamper=_shift_start))
    p = await _staged_phone(w)
    await p.says("Yes, please.")
    [record] = _pending(w)
    assert len(w.escalations) == 1
    assert record.get("companion_escalation_id"), record


async def _two_items(db, spa, monkeypatch, *, link=True):
    _fake, event_id = await _event(db, spa, monkeypatch)
    spa_row = await db.get(SpaAccount, spa.id)
    companion = await esc.record(db, spa_row, "CA-sqlite-call",
                                 {"customer_phone": PHONE, "requested_start": START.isoformat(),
                                  "booking_reference": KEY},
                                 RuntimeError("unclear Google create"))
    details = dict(_details(event_id, uuid.uuid4()), call_sid="CA-sqlite-call")
    record_id = await esc.open_pending_create(db, spa.id, KEY, details)
    await esc.mark_pending_create_unresolved(
        db, record_id, {"companion_escalation_id": str(companion.id)} if link else {}, escalated=True)
    return record_id, companion.id


async def test_reconcile_settles_the_linked_companion_with_history(rubyz_db, monkeypatch):
    db, spa = rubyz_db
    spa_id = spa.id
    record_id, companion_id = await _two_items(db, spa, monkeypatch)
    result = await reconcile_pending_create(db, spa_id, record_id, uuid.uuid4())
    assert result["status"] == "booked", result
    companion = (await db.execute(select(BookingEscalation).where(
        BookingEscalation.id == companion_id))).scalar_one()
    await db.refresh(companion)
    assert companion.status == "booked"
    assert companion.delivery_status != "pending"
    assert companion.details["provider_success_verified"] is True
    assert companion.details["reconciled_appointment_id"] == result["appointment_id"]
    assert [h["action"] for h in companion.history] == ["reconciled_by_pending_create"]
    assert await esc.unresolved_booking(db, spa_id, PHONE, START) is None


async def test_an_unlinked_companion_is_found_only_by_the_exact_call(rubyz_db, monkeypatch):
    """A record written before the link existed: the companion is identified by
    the exact call reference it was recorded under, never by phone or time."""
    db, spa = rubyz_db
    spa_id = spa.id
    record_id, companion_id = await _two_items(db, spa, monkeypatch, link=False)
    spa_row = await db.get(SpaAccount, spa_id)
    unrelated = await esc.record(db, spa_row, "CA-other-call",
                                 {"customer_phone": PHONE, "requested_start": START.isoformat()},
                                 RuntimeError("unrelated"))
    unrelated_id = unrelated.id
    assert (await reconcile_pending_create(db, spa_id, record_id, uuid.uuid4()))["status"] == "booked"
    rows = {r.id: r for r in (await db.execute(select(BookingEscalation).where(
        BookingEscalation.tenant_id == spa_id))).scalars()}
    for row in rows.values():
        await db.refresh(row)
    assert rows[companion_id].status == "booked"
    assert rows[unrelated_id].status == "pending" and rows[unrelated_id].history == []
