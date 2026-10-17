"""Pending provider approval occupies capacity; isolated SQLite, mocked writes."""
import uuid
from datetime import timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1 import appointments as routes
from app.core.tenancy import TenantScope
from app.models import Appointment, AppointmentStatus, Contact
from app.schemas import AppointmentCreate, AppointmentUpdate
from app.services import appointment_booking_service as booking
from app.services.booking_state import arm_verified_proposal
from tests.test_brick1_dashboard_slots import START, HOUR, rows
from tests.test_cara_native_double_booking import rubyz_db, _stage, _slot_start

PENDING = AppointmentStatus.AWAITING_PROVIDER_APPROVAL


async def pending_rows(db, spa):
    pending, other = await rows(db, spa)
    pending.status = PENDING
    pending.booking_provider = "square"
    pending.external_booking_id = "simulated-pending-square"
    await db.commit()
    return pending, other


@pytest.mark.parametrize("same_caller", [True, False])
async def test_dashboard_cannot_create_over_pending_request(rubyz_db, monkeypatch, same_caller):
    db, spa = rubyz_db
    pending, _ = await pending_rows(db, spa)
    contact_id = pending.contact_id
    if not same_caller:
        contact = Contact(id=uuid.uuid4(), tenant_id=spa.id, phone_number="+15550009999")
        db.add(contact)
        await db.commit()
        contact_id = contact.id
    provider = AsyncMock()
    monkeypatch.setattr(routes, "apply_staff_change", provider)
    with pytest.raises(HTTPException) as caught:
        await routes.create_appointment(AppointmentCreate(contact_id=contact_id, title="Visit",
            start_time=START, end_time=START + HOUR), scope=TenantScope.for_tenant(spa.id), db=db)
    assert caught.value.status_code == 409
    provider.assert_not_awaited()
    assert len(list((await db.execute(select(Appointment))).scalars())) == 2


async def test_dashboard_move_into_pending_slot_is_refused_without_write(rubyz_db, monkeypatch):
    db, spa = rubyz_db
    pending, moving = await pending_rows(db, spa)
    provider = AsyncMock()
    monkeypatch.setattr(routes, "apply_staff_change", provider)
    with pytest.raises(HTTPException) as caught:
        await routes.update_appointment(moving.id, AppointmentUpdate(start_time=START, end_time=START + HOUR),
            scope=TenantScope.for_tenant(spa.id), db=db)
    assert caught.value.status_code == 409
    provider.assert_not_awaited()
    await db.refresh(moving)
    assert moving.start_time.replace(tzinfo=timezone.utc) == START + 2 * HOUR
    assert pending.status == PENDING


@pytest.mark.parametrize("status", [AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED])
async def test_pending_request_cannot_be_promoted_without_provider_approval(rubyz_db, monkeypatch, status):
    db, spa = rubyz_db
    pending, _ = await pending_rows(db, spa)
    provider = AsyncMock()
    monkeypatch.setattr(routes, "apply_staff_change", provider)
    with pytest.raises(HTTPException) as caught:
        await routes.update_appointment(pending.id, AppointmentUpdate(status=status),
            scope=TenantScope.for_tenant(spa.id), db=db)
    assert caught.value.status_code == 409
    provider.assert_not_awaited()
    await db.refresh(pending)
    assert pending.status == PENDING


@pytest.mark.parametrize("method", ["patch", "delete"])
async def test_pending_request_can_be_cancelled_once(rubyz_db, monkeypatch, method):
    db, spa = rubyz_db
    pending, _ = await pending_rows(db, spa)
    provider = AsyncMock()
    monkeypatch.setattr(routes, "apply_staff_change", provider)
    scope = TenantScope.for_tenant(spa.id)
    if method == "patch":
        await routes.update_appointment(pending.id, AppointmentUpdate(status=AppointmentStatus.CANCELLED), scope=scope, db=db)
    else:
        await routes.cancel_appointment(pending.id, scope=scope, db=db)
    provider.assert_awaited_once()
    assert provider.await_args.kwargs["cancel"] is True
    await db.refresh(pending)
    assert pending.status == AppointmentStatus.CANCELLED


@pytest.mark.parametrize("same_caller", [True, False])
async def test_voice_confirm_rechecks_pending_slot_and_never_creates_duplicate(rubyz_db, same_caller):
    db, spa = rubyz_db
    start = _slot_start()
    caller, staged = await _stage(db, spa, start, 1)
    assert staged.outcome == booking.BookingOutcome.DRAFT
    contact = (await db.execute(select(Contact).where(Contact.tenant_id == spa.id,
        Contact.phone_number == caller.from_number))).scalar_one() if same_caller else Contact(
            id=uuid.uuid4(), tenant_id=spa.id, phone_number="+15550009999")
    # A pending request arrives after the offered slot, before this caller's yes.
    db.add(contact)
    await db.flush()
    pending = Appointment(id=uuid.uuid4(), tenant_id=spa.id, contact_id=contact.id,
        title="Signature Facial", start_time=start.astimezone(timezone.utc),
        end_time=(start + HOUR).astimezone(timezone.utc), status=PENDING,
        booking_provider="square", external_booking_id="simulated-pending-square")
    db.add(pending)
    await db.commit()
    arm_verified_proposal(caller)
    result = await booking.confirm_booking(db, caller)
    assert result.outcome not in {booking.BookingOutcome.BOOKED, booking.BookingOutcome.RESCHEDULED}, result.message
    assert len(list((await db.execute(select(Appointment))).scalars())) == 1
    await db.refresh(pending)
    assert pending.status == PENDING

async def test_square_move_preserves_provider_pending_over_form_confirmation(rubyz_db, monkeypatch):
    db, spa = rubyz_db
    _, moving = await rows(db, spa)
    moving.booking_provider = "square"
    moving.external_booking_id = "simulated-square-moving"
    await db.commit()

    async def provider_change(db, scope, row, **kwargs):
        row.status = PENDING

    provider = AsyncMock(side_effect=provider_change)
    monkeypatch.setattr(routes, "apply_staff_change", provider)
    result = await routes.update_appointment(moving.id,
        AppointmentUpdate(start_time=START + 4 * HOUR, end_time=START + 5 * HOUR,
                          status=AppointmentStatus.CONFIRMED),
        scope=TenantScope.for_tenant(spa.id), db=db)
    provider.assert_awaited_once()
    assert result.status == PENDING, "dashboard form overwrote Square's pending approval"
    await db.refresh(moving)
    assert moving.status == PENDING


def install_pending_square(monkeypatch, square):
    from app.services.booking_adapters.providers.square import SquareAdapter
    real_request = SquareAdapter._request

    async def request(self, method, path, **kwargs):
        response = await real_request(self, method, path, **kwargs)
        if method == "POST" and path == "/v2/bookings":
            response["booking"]["status"] = "PENDING"
            square.bookings[response["booking"]["id"]]["status"] = "PENDING"
        return response

    monkeypatch.setattr(SquareAdapter, "_request", request)


@pytest.mark.parametrize("same_caller", [False, True])
async def test_second_square_caller_cannot_book_a_locally_pending_slot(rubyz_db, monkeypatch, same_caller):
    from tests.test_codex_brick1_tenant_boundaries import setup_tenants, session_for, stage
    db, spa, _, squares = await setup_tenants(rubyz_db, monkeypatch)
    square = squares["fake-cleaning-token"]
    install_pending_square(monkeypatch, square)
    first = session_for(spa, "pending-first", phone="+15550007771")
    second = session_for(spa, "pending-second", phone="+15550007771" if same_caller else "+15550007772")
    for caller in (first, second):
        await stage(db, spa, caller)
        arm_verified_proposal(caller)
    accepted_request = await booking.confirm_booking(db, first)
    assert accepted_request.outcome.value == "awaiting_provider_approval", accepted_request.message
    late = await booking.confirm_booking(db, second)
    assert len(square.bookings) == 1, "SECOND SQUARE BOOKING IN PENDING SLOT"
    assert len(list((await db.execute(select(Appointment))).scalars())) == 1
    assert late.outcome.value in ({"conflict", "awaiting_provider_approval"} if same_caller else {"conflict"}), late.message


async def test_pending_hold_cannot_be_bypassed_by_business_capacity(rubyz_db):
    db, spa = rubyz_db
    await pending_rows(db, spa)
    assert await booking._has_conflict(db, TenantScope.for_tenant(spa.id), START, START + HOUR, capacity=3), "PENDING HOLD BYPASSED BY CAPACITY"
