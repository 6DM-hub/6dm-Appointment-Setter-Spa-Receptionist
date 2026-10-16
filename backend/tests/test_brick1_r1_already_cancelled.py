"""R1 (owner decision, Option A): cancelling an appointment that is already
cancelled is refused with 409 "This appointment is already cancelled." - on the
dashboard DELETE route and on PATCH status=CANCELLED - with no provider call
and no write. Real disposable SQLite rows; the provider boundary is a mock.
"""
import uuid
from datetime import timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.v1 import appointments as routes
from app.core.tenancy import TenantScope
from app.models import AppointmentStatus
from app.schemas import AppointmentUpdate
from tests.test_brick1_dashboard_slots import HOUR, START, rows
from tests.test_cara_native_double_booking import rubyz_db  # noqa: F401  (fixture)

ALREADY = "This appointment is already cancelled."


async def _cancelled_row(db, spa):
    _, row = await rows(db, spa)
    row.status = AppointmentStatus.CANCELLED
    row.external_booking_id = "simulated-external-id"
    await db.commit()
    await db.refresh(row)
    return row


def _count_commits(monkeypatch, db):
    calls = []
    real = db.commit

    async def commit():
        calls.append(1)
        await real()

    monkeypatch.setattr(db, "commit", commit)
    return calls


@pytest.mark.parametrize("route", ["delete", "patch_status_cancelled"])
async def test_cancelling_an_already_cancelled_appointment_is_409_without_any_write(rubyz_db, monkeypatch, route):
    db, spa = rubyz_db
    row = await _cancelled_row(db, spa)
    updated_before = row.updated_at
    provider = AsyncMock()
    monkeypatch.setattr(routes, "apply_staff_change", provider)
    commits = _count_commits(monkeypatch, db)
    scope = TenantScope.for_tenant(spa.id)
    with pytest.raises(HTTPException) as exc:
        if route == "delete":
            await routes.cancel_appointment(row.id, scope=scope, db=db)
        else:
            await routes.update_appointment(
                row.id, AppointmentUpdate(status=AppointmentStatus.CANCELLED), scope=scope, db=db)
    assert exc.value.status_code == 409
    assert exc.value.detail == ALREADY
    provider.assert_not_awaited()
    assert commits == [], "an already-cancelled appointment must not be written"
    await db.refresh(row)
    assert row.status is AppointmentStatus.CANCELLED
    assert row.updated_at == updated_before


@pytest.mark.parametrize("route", ["delete", "patch_status_cancelled"])
async def test_first_cancel_still_sends_exactly_one_provider_cancel(rubyz_db, monkeypatch, route):
    db, spa = rubyz_db
    _, row = await rows(db, spa)
    row.external_booking_id = "simulated-external-id"
    await db.commit()
    provider = AsyncMock()
    monkeypatch.setattr(routes, "apply_staff_change", provider)
    scope = TenantScope.for_tenant(spa.id)
    if route == "delete":
        await routes.cancel_appointment(row.id, scope=scope, db=db)
    else:
        await routes.update_appointment(
            row.id, AppointmentUpdate(status=AppointmentStatus.CANCELLED), scope=scope, db=db)
    provider.assert_awaited_once()
    assert provider.await_args.kwargs.get("cancel") is True
    await db.refresh(row)
    assert row.status is AppointmentStatus.CANCELLED


@pytest.mark.parametrize("route", ["delete", "patch_status_cancelled"])
async def test_another_tenants_cancelled_appointment_is_404_not_409(rubyz_db, monkeypatch, route):
    db, spa = rubyz_db
    row = await _cancelled_row(db, spa)
    provider = AsyncMock()
    monkeypatch.setattr(routes, "apply_staff_change", provider)
    other = TenantScope.for_tenant(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        if route == "delete":
            await routes.cancel_appointment(row.id, scope=other, db=db)
        else:
            await routes.update_appointment(
                row.id, AppointmentUpdate(status=AppointmentStatus.CANCELLED), scope=other, db=db)
    assert exc.value.status_code == 404
    provider.assert_not_awaited()


async def test_other_edits_of_a_cancelled_row_are_unchanged(rubyz_db, monkeypatch):
    """Only re-cancelling is refused; e.g. a note-only edit keeps working."""
    db, spa = rubyz_db
    row = await _cancelled_row(db, spa)
    provider = AsyncMock()
    monkeypatch.setattr(routes, "apply_staff_change", provider)
    out = await routes.update_appointment(
        row.id, AppointmentUpdate(description="Customer called to say thanks."),
        scope=TenantScope.for_tenant(spa.id), db=db)
    provider.assert_not_awaited()
    assert out.status is AppointmentStatus.CANCELLED
    assert out.start_time.replace(tzinfo=timezone.utc) == START + 2 * HOUR

