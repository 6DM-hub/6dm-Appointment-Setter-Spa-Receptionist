"""Completed visits and no-shows cannot become provider cancellations."""
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.v1 import appointments as routes
from app.core.tenancy import TenantScope
from app.models import AppointmentStatus
from app.schemas import AppointmentUpdate
from tests.test_brick1_dashboard_slots import rows
from tests.test_brick1_r1_already_cancelled import _count_commits
from tests.test_cara_native_double_booking import rubyz_db  # noqa: F401


async def check_terminal_cancel(db, spa, monkeypatch, terminal, method):
    _, row = await rows(db, spa)
    row.status = terminal
    row.external_booking_id = "test-only-external-booking"
    await db.commit()
    await db.refresh(row)
    before = (row.status, row.start_time, row.end_time, row.updated_at)
    provider = AsyncMock()
    monkeypatch.setattr(routes, "apply_staff_change", provider)
    commits = _count_commits(monkeypatch, db)
    with pytest.raises(HTTPException) as caught:
        if method == "patch":
            await routes.update_appointment(row.id, AppointmentUpdate(status=AppointmentStatus.CANCELLED),
                                            scope=TenantScope.for_tenant(spa.id), db=db)
        else:
            await routes.cancel_appointment(row.id, scope=TenantScope.for_tenant(spa.id), db=db)
    assert caught.value.status_code == 409
    assert caught.value.detail == "An inactive appointment cannot be cancelled"
    provider.assert_not_awaited()
    assert commits == []
    await db.refresh(row)
    assert (row.status, row.start_time, row.end_time, row.updated_at) == before


@pytest.mark.parametrize("terminal", [AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW])
@pytest.mark.parametrize("method", ["patch", "delete"])
async def test_terminal_cancel_is_refused_without_writes(rubyz_db, monkeypatch, terminal, method):
    await check_terminal_cancel(*rubyz_db, monkeypatch, terminal, method)
