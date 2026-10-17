"""Real PostgreSQL worker-death boundary; all Square requests are simulated."""
import asyncio
import copy
import uuid

import pytest
from sqlalchemy import select

from app.models import Appointment, Contact, SpaAccount
from app.models.booking_escalation import BookingEscalation
from app.services import appointment_booking_service as booking
from app.services import booking_escalations as esc
from app.services.booking_adapters.providers.square import SquareAdapter
from app.services.booking_state import arm_verified_proposal
from app.services.google_create_reconciliation import reconcile_pending_create
from tests.test_codex_brick1_tenant_boundaries import setup_tenants, session_for, stage
from tests_postgres.test_postgres_booking_races import pg  # noqa: F401


async def test_square_worker_death_then_concurrent_reconcile_preserves_one_booking(pg, monkeypatch):
    maker, spas = pg
    async with maker() as db:
        # The fixture tenant is detached; load it here so setup commits its menu.
        tenant = await db.get(SpaAccount, spas[0].id)
        _, spa, _, squares = await setup_tenants((db, tenant), monkeypatch)
    tenant_id = spa.id
    square = squares["fake-cleaning-token"]
    request = SquareAdapter._request
    sent = []
    die = True

    async def transport(self, method, path, **kwargs):
        nonlocal die
        is_create = method == "POST" and path == "/v2/bookings"
        if is_create:
            sent.append(copy.deepcopy(kwargs["json"]))
            # Prove the record is visible to another transaction before send.
            async with maker() as observer:
                records = list((await observer.execute(select(BookingEscalation).where(
                    BookingEscalation.tenant_id == tenant_id,
                    BookingEscalation.category == esc.PENDING_CREATE))).scalars())
                assert len(records) == 1, "NO DURABLE ATTEMPT BEFORE SQUARE WRITE"
        result = await request(self, method, path, **kwargs)
        if is_create and die:
            die = False
            raise asyncio.CancelledError("worker died after provider accepted")
        return result

    monkeypatch.setattr(SquareAdapter, "_request", transport)
    call = session_for(spa, "square-worker-death", phone="+15550007878")
    async with maker() as db:
        await stage(db, spa, call)
    arm_verified_proposal(call)
    with pytest.raises(asyncio.CancelledError):
        async with maker() as db:
            await booking.confirm_booking(db, call)
    assert len(square.bookings) == 1
    async with maker() as db:
        [record] = list((await db.execute(select(BookingEscalation).where(
            BookingEscalation.tenant_id == tenant_id, BookingEscalation.category == esc.PENDING_CREATE))).scalars())
        rid = record.id
        assert list((await db.execute(select(Appointment).where(Appointment.tenant_id == tenant_id))).scalars()) == []
        assert list((await db.execute(select(Contact).where(Contact.tenant_id == tenant_id))).scalars()) == []
        await esc.mark_pending_create_unresolved(db, rid, {}, escalated=False)

    async def recover():
        async with maker() as db:
            return await reconcile_pending_create(db, tenant_id, rid, uuid.uuid4())
    results = await asyncio.gather(recover(), recover())
    assert sorted(r["status"] for r in results) == ["already_resolved", "booked"]
    assert sent == [sent[0], sent[0]]
    assert len(square.bookings) == 1
    async with maker() as db:
        [row] = list((await db.execute(select(Appointment).where(Appointment.tenant_id == tenant_id))).scalars())
        contact = await db.get(Contact, row.contact_id)
        assert contact is not None and contact.tenant_id == tenant_id
        assert row.external_booking_id == square.active()[0]["id"]
