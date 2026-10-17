"""Square restart reconciliation uses saved request identity, never list matching."""
import copy
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import Appointment, AppointmentStatus, BookingProvider, Contact
from app.models.booking_escalation import BookingEscalation
from app.services import booking_escalations as esc
from app.services.booking_config import encrypt_config
from app.services.booking_adapters.providers.square import SquareAdapter
from app.services.google_create_reconciliation import reconcile_pending_create
from tests.test_brick1_square_lost_create import request_payload
from tests.test_cara_native_double_booking import rubyz_db  # noqa: F401


@pytest.mark.parametrize("mode", ["replay", "known_id", "pending", "cancelled", "wrong_id", "config_changed", "linked_pending", "linked_accepted"])
async def test_reconcile_square_requires_exact_request_and_current_acceptance(rubyz_db, monkeypatch, mode):
    db, spa = rubyz_db
    tenant_id = spa.id
    spa.booking_provider = BookingProvider.SQUARE
    spa.booking_config = {"access_token": "fake-only", "location_id": "location-one", "environment": "sandbox"}
    await db.commit()
    adapter = SquareAdapter("Test", spa.booking_config)
    payload = request_payload()
    calls = []
    event = {**copy.deepcopy(payload["booking"]), "id": "booking-one", "status": "ACCEPTED"}

    async def request(self, method, path, **kwargs):
        calls.append((method, path, copy.deepcopy(kwargs)))
        if method == "POST":
            assert path == "/v2/bookings" and kwargs["json"] == payload
            return {"booking": copy.deepcopy(event)}
        assert path == "/v2/bookings/booking-one"
        current = copy.deepcopy(event)
        if mode in {"pending", "linked_pending"}: current["status"] = "PENDING"
        if mode == "cancelled": current["status"] = "CANCELLED_BY_CUSTOMER"
        if mode == "wrong_id": current["id"] = "unrelated-booking"
        return {"booking": current}

    monkeypatch.setattr(SquareAdapter, "_request", request)
    start = datetime.fromisoformat(payload["booking"]["start_at"].replace("Z", "+00:00"))
    details = {"booking_reference": "test-intent", "provider": "square",
               "square_request": encrypt_config(payload), "square_environment": "sandbox",
               "square_api_version": adapter.api_version, "requested_start": start.isoformat(),
               "requested_end": (start + timedelta(hours=1)).isoformat(),
               "contact_phone": "+15550004444", "customer_phone": "+15550004444", "title": "Visit"}
    if mode == "known_id":
        details.update(external_booking_id="booking-one", provider_success_verified=True)
    if mode == "config_changed": details["square_environment"] = "production"
    if mode.startswith("linked_"):
        contact = Contact(tenant_id=tenant_id, phone_number="+15550004444")
        db.add(contact)
        await db.flush()
        db.add(Appointment(tenant_id=tenant_id, contact_id=contact.id, title="Visit",
            start_time=start, end_time=start + timedelta(hours=1), booking_provider="square",
            external_booking_id="booking-one", booking_intent_key="test-intent",
            status=(AppointmentStatus.SCHEDULED if mode == "linked_pending" else AppointmentStatus.AWAITING_PROVIDER_APPROVAL)))
        await db.commit()
    rid = await esc.open_pending_create(db, tenant_id, "test-intent", details)
    await esc.mark_pending_create_unresolved(db, rid, {}, escalated=False)
    result = await reconcile_pending_create(db, tenant_id, rid, uuid.uuid4())
    rows = list((await db.execute(select(Appointment).where(Appointment.tenant_id == tenant_id))).scalars())
    if mode in {"replay", "known_id", "pending", "linked_pending", "linked_accepted"}:
        assert result["status"] == ("awaiting_provider_approval" if mode in {"pending", "linked_pending"} else "booked")
        assert rows[0].status == ("awaiting_provider_approval" if mode in {"pending", "linked_pending"} else "scheduled")
        assert len(rows) == 1 and rows[0].external_booking_id == "booking-one"
        assert rows[0].booking_provider == "square"
        assert [c[0] for c in calls] == (["GET"] if mode == "known_id" else ["POST", "GET"])
        again = await reconcile_pending_create(db, tenant_id, rid, uuid.uuid4())
        assert again["status"] == "already_resolved"
    else:
        assert result["status"] == "unknown" and rows == []
        pending = await db.get(BookingEscalation, rid)
        assert pending.status == "pending"
        if mode == "config_changed": assert calls == []
