"""Dashboard safety against real disposable SQLite rows; no provider network."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from app.api.v1 import appointments as routes
from app.core.tenancy import TenantScope
from app.models import Appointment, AppointmentStatus, Contact
from app.schemas import AppointmentCreate, AppointmentUpdate
from tests.test_cara_native_double_booking import rubyz_db

START = datetime(2030, 1, 7, 14, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


async def rows(db, spa):
    contact = Contact(id=uuid.uuid4(), tenant_id=spa.id, phone_number="+15550001234")
    db.add(contact)
    await db.flush()
    result = []
    for start in (START, START + 2 * HOUR):
        row = Appointment(id=uuid.uuid4(), tenant_id=spa.id, contact_id=contact.id,
                          title="Inspection", start_time=start, end_time=start + HOUR,
                          status=AppointmentStatus.CONFIRMED)
        db.add(row)
        result.append(row)
    await db.commit()
    return result


async def test_move_into_occupied_slot_is_409_and_does_not_write(rubyz_db, monkeypatch):
    db, spa = rubyz_db
    first, second = await rows(db, spa)
    provider = AsyncMock()
    monkeypatch.setattr(routes, "apply_staff_change", provider)
    with pytest.raises(HTTPException) as exc:
        await routes.update_appointment(second.id, AppointmentUpdate(start_time=START, end_time=START + HOUR),
                                        scope=TenantScope.for_tenant(spa.id), db=db)
    assert exc.value.status_code == 409
    provider.assert_not_awaited()
    await db.refresh(second)
    assert second.start_time.replace(tzinfo=timezone.utc) == START + 2 * HOUR


@pytest.mark.parametrize("status", [AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW])
@pytest.mark.parametrize("operation", ["move", "reactivate"])
async def test_inactive_booking_cannot_move_or_reactivate(rubyz_db, monkeypatch, status, operation):
    db, spa = rubyz_db
    _, row = await rows(db, spa)
    row.status = status
    await db.commit()
    provider = AsyncMock()
    monkeypatch.setattr(routes, "apply_staff_change", provider)
    payload = (AppointmentUpdate(status=AppointmentStatus.SCHEDULED) if operation == "reactivate"
               else AppointmentUpdate(start_time=START + 4 * HOUR, end_time=START + 5 * HOUR))
    with pytest.raises(HTTPException) as exc:
        await routes.update_appointment(row.id, payload, scope=TenantScope.for_tenant(spa.id), db=db)
    assert exc.value.status_code == 409
    provider.assert_not_awaited()


@pytest.mark.parametrize("rubyz_db", [2], indirect=True)
async def test_create_uses_capacity_and_refuses_when_full(rubyz_db):
    db, spa = rubyz_db
    first, _ = await rows(db, spa)
    payload = AppointmentCreate(contact_id=first.contact_id, title="Inspection", start_time=START, end_time=START + HOUR)
    scope = TenantScope.for_tenant(spa.id)
    added = await routes.create_appointment(payload, scope=scope, db=db)
    assert added.id != first.id
    with pytest.raises(HTTPException) as exc:
        await routes.create_appointment(payload, scope=scope, db=db)
    assert exc.value.status_code == 409


async def test_adjacent_move_and_unchanged_slot_are_allowed(rubyz_db):
    db, spa = rubyz_db
    first, second = await rows(db, spa)
    scope = TenantScope.for_tenant(spa.id)
    result = await routes.update_appointment(second.id, AppointmentUpdate(start_time=START + HOUR, end_time=START + 2 * HOUR), scope=scope, db=db)
    assert result.start_time.replace(tzinfo=timezone.utc) == START + HOUR
    await routes.update_appointment(first.id, AppointmentUpdate(description="No time change"), scope=scope, db=db)
