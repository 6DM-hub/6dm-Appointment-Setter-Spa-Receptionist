"""HTTP role boundary: a read-only user must never reach calendar mutation."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import appointments as routes
from app.core.database import get_db
from app.main import app
from app.models import Appointment, AppointmentStatus, SpaAccount, UserRole
from tests.conftest import make_spa, make_user


@pytest.mark.parametrize("method", ["patch", "delete"])
@pytest.mark.parametrize("role", [UserRole.SPA_STAFF, UserRole.SPA_ADMIN, UserRole.SUPER_ADMIN])
def test_appointment_mutation_requires_manager(client, principal, monkeypatch, method, role):
    tenant = uuid.uuid4()
    principal.user = make_user(role, tenant if role != UserRole.SUPER_ADMIN else None)
    start = datetime(2030, 1, 7, 14, tzinfo=timezone.utc)
    row = Appointment(
        id=uuid.uuid4(), tenant_id=tenant, user_id=None, contact_id=uuid.uuid4(),
        title="Equipment Inspection", start_time=start, end_time=start + timedelta(hours=1),
        status=AppointmentStatus.CONFIRMED, booking_provider="google_calendar",
        external_booking_id="simulated-event", created_at=start, updated_at=start,
        card_status="not_required",
    )
    async def get(model, pk):
        if model is Appointment:
            return row
        if model is SpaAccount:
            return make_spa(id=tenant)
    db = SimpleNamespace(get=AsyncMock(side_effect=get), commit=AsyncMock(), refresh=AsyncMock())
    async def dependency():
        yield db
    app.dependency_overrides[get_db] = dependency
    mutation = AsyncMock()
    monkeypatch.setattr(routes, "apply_staff_change", mutation)
    headers = {"X-Tenant-Id": str(tenant)} if role == UserRole.SUPER_ADMIN else {}
    url = f"/api/v1/appointments/{row.id}"
    if method == "patch":
        response = client.patch(url, json={"start_time": (start + timedelta(hours=2)).isoformat(),
                                          "end_time": (start + timedelta(hours=3)).isoformat()}, headers=headers)
    else:
        response = client.delete(url, headers=headers)
    if role == UserRole.SPA_STAFF:
        assert response.status_code == 403, (response.status_code, response.text, mutation.await_count)
        mutation.assert_not_awaited()
        db.commit.assert_not_awaited()
    else:
        assert response.status_code == (200 if method == "patch" else 204), response.text
        mutation.assert_awaited_once()
