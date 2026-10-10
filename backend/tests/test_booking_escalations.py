from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import copy
import uuid
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_cara_manager import data
from app.models.booking_escalation import BookingEscalation
from app.services import booking_escalations as e
from app.services.square_booking_recovery import create_with_recovery, classify
from app.services.booking_adapters.base import BookingProviderError


def payload():
    return {"idempotency_key": "same-key", "booking": {"location_id": "loc", "customer_id": "customer",
        "start_at": "2026-11-01T16:00:00Z", "appointment_segments": [{"team_member_id": "staff", "duration_minutes": 60,
        "service_variation_id": "service", "service_variation_version": 1}]}}


@pytest.mark.asyncio
async def test_timeout_after_square_commit_recovers_without_second_write():
    p = payload()
    booking = {**p["booking"], "id": "real-booking", "status": "ACCEPTED"}
    request = AsyncMock(side_effect=[TimeoutError(), {"bookings": [booking]}])
    result = await create_with_recovery(SimpleNamespace(_request=request), p)
    assert result["booking"]["id"] == "real-booking"
    assert [call.args[0] for call in request.call_args_list] == ["POST", "GET"]


@pytest.mark.asyncio
async def test_safe_retry_preserves_exact_payload():
    request = AsyncMock(side_effect=[BookingProviderError("busy", retryable=True), {"bookings": []}, {"booking": {"id": "ok"}}])
    p = payload()
    await create_with_recovery(SimpleNamespace(_request=request), p)
    assert request.call_args_list[0].kwargs["json"] == request.call_args_list[2].kwargs["json"] == p


@pytest.mark.asyncio
async def test_retries_bounded_and_every_uncertain_write_read_back():
    request = AsyncMock(side_effect=[TimeoutError(), {"bookings": []}, TimeoutError(), {"bookings": []}])
    with pytest.raises(BookingProviderError) as error:
        await create_with_recovery(SimpleNamespace(_request=request), payload())
    assert error.value.code == "BOOKING_OUTCOME_UNKNOWN"
    assert request.await_count == 4


@pytest.mark.asyncio
async def test_missing_read_permission_prevents_retry():
    request = AsyncMock(side_effect=[TimeoutError(), BookingProviderError("forbidden", code="FORBIDDEN")])
    with pytest.raises(BookingProviderError, match="cannot be verified"):
        await create_with_recovery(SimpleNamespace(_request=request), payload())
    assert request.await_count == 2


@pytest.mark.parametrize("code,category", [("FORBIDDEN", "permissions"), ("VISIT_CONFLICT", "conflict"), ("SLOT_UNAVAILABLE", "unavailable"), ("LOCATION_TIMEZONE", "configuration")])
@pytest.mark.asyncio
async def test_permanent_errors_never_retry(code, category):
    error = BookingProviderError("problem", code=code)
    assert classify(error) == category
    request = AsyncMock(side_effect=error)
    with pytest.raises(BookingProviderError):
        await create_with_recovery(SimpleNamespace(_request=request), payload())
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_escalation_persistence_urgency_and_alert_dedup(data):
    db, spa, owner, _, customer = data
    details = {"customer_phone": customer.phone_number, "requested_start": datetime.now(timezone.utc).isoformat(), "services": "Facial", "duration_minutes": 60}
    row = await e.record(db, spa, "call-1", details, TimeoutError())
    assert row.priority == "urgent" and row.status == "pending"
    again = await e.record(db, spa, "call-1", details, TimeoutError())
    assert row.id == again.id
    send = AsyncMock(return_value={"status": "accepted"})
    await e.deliver(db, spa, row, send)
    await e.deliver(db, spa, row, send)
    assert send.await_count == 1
    assert "URGENT" in send.call_args.args[2]
    assert len(list((await db.execute(select(BookingEscalation))).scalars())) == 1


@pytest.mark.asyncio
async def test_alert_failure_persists_and_is_not_blindly_retried(data):
    db, spa, *_ = data
    row = await e.record(db, spa, "call-2", {}, TimeoutError())
    send = AsyncMock(side_effect=TimeoutError())
    assert (await e.deliver(db, spa, row, send))["status"] == "delivery_unknown"
    await e.deliver(db, spa, row, send)
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_staff_resolution_is_tenant_scoped_versioned_and_audited(data):
    db, spa, owner, *_ = data
    row = await e.record(db, spa, "call-3", {}, BookingProviderError("denied", code="FORBIDDEN"))
    with pytest.raises(HTTPException) as err:
        await e.resolve(db, uuid.uuid4(), row.id, owner.id, "contacted", row.version, "")
    assert err.value.status_code == 404
    with pytest.raises(HTTPException):
        await e.resolve(db, spa.id, row.id, owner.id, "booked", row.version, "")
    row = await e.resolve(db, spa.id, row.id, owner.id, "contacted", row.version, "Called customer")
    assert row.history[-1]["to"] == "contacted"
    with pytest.raises(HTTPException):
        await e.resolve(db, spa.id, row.id, owner.id, "pending", row.version - 1, "")
    row = await e.resolve(db, spa.id, row.id, owner.id, "booked", row.version, "Booked in Square", "square-1")
    assert not row.details["provider_success_verified"]


@pytest.mark.asyncio
async def test_customer_followup_requires_consent_and_deduplicates(data):
    db, spa, owner, _, customer = data
    row = await e.record(db, spa, "call-4", {"customer_phone": customer.phone_number}, BookingProviderError("Permission denied", code="FORBIDDEN"))
    row = await e.resolve(db, spa.id, row.id, owner.id, "unable_to_book", row.version, "No suitable opening")
    send = AsyncMock(return_value="SM-test")
    with pytest.raises(HTTPException):
        await e.customer_followup(db, spa.id, row.id, owner.id, "", send)
    await e.customer_followup(db, spa.id, row.id, owner.id, "Customer requested SMS follow-up on call", send)
    await e.customer_followup(db, spa.id, row.id, owner.id, "Customer requested SMS follow-up on call", send)
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_customer_optout_prevents_followup(data):
    db, spa, owner, _, customer = data
    customer.extra_metadata = {"sms_opt_out": True}
    row = await e.record(db, spa, "call-5", {"customer_phone": customer.phone_number}, BookingProviderError("Permission denied", code="FORBIDDEN"))
    row = await e.resolve(db, spa.id, row.id, owner.id, "unable_to_book", row.version, "")
    send = AsyncMock()
    with pytest.raises(HTTPException):
        await e.customer_followup(db, spa.id, row.id, owner.id, "Consent", send)
    send.assert_not_called()


from tests.test_multi_service_visit import visit


@pytest.mark.asyncio
async def test_actual_booking_failure_rolls_back_and_creates_attention_request(data, visit, monkeypatch):
    from app.services import appointment_booking_service as booking
    from app.models import Appointment, AppointmentStatus
    from app.services.call_state import CallSession
    from app.services.grok_service import AppointmentIntent
    from app.services.booking_state import arm_verified_proposal
    db, spa, owner, _, customer = data
    router, ctx, state = visit
    spa.services, spa.staff, spa.business_hours = router.spa.services, router.spa.staff, router.spa.business_hours
    router.spa, router.calendar_label, router.default_title = spa, "mock Square", "Visit"
    tid, phone = str(spa.id), customer.phone_number
    session = CallSession("failed-db-call", "inbound", phone, "test", tenant_id=tid, timezone=spa.timezone)
    monkeypatch.setattr(booking, "get_booking_adapter", lambda **kwargs: router)
    monkeypatch.setattr(booking, "_load_spa", AsyncMock(return_value=spa))
    sender = AsyncMock(return_value={"status": "accepted"})
    monkeypatch.setattr("app.services.escalation_mail.send_staff", sender)
    request = AppointmentIntent(intent="schedule", caller_name="Test Alice", requested_start_iso=ctx.start.isoformat(), requested_services=["European facial", "Deep tissue massage"])
    assert (await booking.stage_booking(db, session, request)).outcome == booking.BookingOutcome.DRAFT
    arm_verified_proposal(session)
    router.create_booking = AsyncMock(side_effect=BookingProviderError("Missing write permission", code="FORBIDDEN"))
    result = await booking.confirm_booking(db, session)
    assert result.outcome == booking.BookingOutcome.ERROR
    assert not (await db.execute(select(Appointment).where(Appointment.status == AppointmentStatus.SCHEDULED))).first()
    row = (await db.execute(select(BookingEscalation))).scalar_one()
    assert row.category == "permissions"
    assert row.details["duration_minutes"] == 150
    assert row.details["customer_phone"] == phone
    assert row.details["slot"]["visit_segments"]
    await e.dispatch_pending(db)
    assert sender.await_count == 1
    assert (await booking.confirm_booking(db, session)).outcome == booking.BookingOutcome.ERROR
    assert router.create_booking.await_count == 1


@pytest.mark.asyncio
async def test_email_and_sms_delivery_independent(monkeypatch):
    from app.services import escalation_mail as mail
    sms = AsyncMock(side_effect=TimeoutError())
    email = AsyncMock(return_value="accepted")
    monkeypatch.setattr("app.services.twilio_service.twilio_service.send_sms", sms)
    monkeypatch.setattr(mail, "send_email", email)
    spa = SimpleNamespace(notification_settings={"booking_escalation": {"sms_destinations": ["+19995550100"], "email_destinations": ["staff@example.test"]}})
    result = await mail.send_staff(spa, "booking_failed", "Needs Staff Attention")
    assert [item["status"] for item in result["deliveries"]] == ["delivery_unknown", "accepted"]


@pytest.mark.asyncio
async def test_no_email_credentials_reports_not_configured(monkeypatch):
    from app.services import escalation_mail as mail
    monkeypatch.setattr(mail.settings, "STAFF_SMTP_HOST", "")
    assert await mail.send_email("staff@example.test", "test") == "not_configured"


@pytest.mark.asyncio
async def test_unknown_outcome_survives_new_call_but_not_other_tenant(data):
    db, spa, owner, _, customer = data
    start = datetime(2027, 1, 15, 20, tzinfo=timezone.utc)
    row = await e.record(db, spa, "old-call", {"customer_phone": customer.phone_number,
        "requested_start": start.isoformat()}, BookingProviderError("unknown", code="BOOKING_OUTCOME_UNKNOWN"))
    assert (await e.unresolved_booking(db, spa.id, customer.phone_number, start)).id == row.id
    assert await e.unresolved_booking(db, uuid.uuid4(), customer.phone_number, start) is None
    assert await e.unresolved_booking(db, spa.id, "+19995550199", start) is None


@pytest.mark.asyncio
async def test_calendar_wrapper_preserves_provider_code():
    from app.services.appointment_booking_service import _calendar_call
    error = BookingProviderError("scope missing", code="INSUFFICIENT_SCOPES")
    with pytest.raises(BookingProviderError) as caught:
        await _calendar_call("create_booking", "test", AsyncMock(side_effect=error)())
    assert caught.value is error


@pytest.mark.asyncio
async def test_readback_does_not_accept_wrong_staff_or_duration():
    from app.services.square_booking_recovery import reconcile
    p = payload()
    wrong = {**copy.deepcopy(p["booking"]), "id": "other", "status": "ACCEPTED"}
    wrong["appointment_segments"][0]["team_member_id"] = "wrong"
    request = AsyncMock(return_value={"bookings": [wrong]})
    assert await reconcile(SimpleNamespace(_request=request), p) is None


@pytest.mark.asyncio
async def test_same_day_urgency_uses_business_zone(data, monkeypatch):
    db, spa, *_ = data
    monkeypatch.setattr(e, "now", lambda: datetime(2026, 10, 10, 2, tzinfo=timezone.utc))
    # 9 PM Oct 9 in Dallas. UTC calendar day differs.
    row = await e.record(db, spa, "zone-call", {"requested_start": "2026-10-09T22:00:00-05:00"}, TimeoutError())
    assert row.priority == "urgent"


@pytest.mark.asyncio
async def test_staff_notifications_disabled_without_losing_request(data):
    db, spa, *_ = data
    spa.notification_settings = {"booking_escalation": {"notifications_enabled": False}}
    row = await e.record(db, spa, "disabled-call", {}, TimeoutError())
    sender = AsyncMock()
    await e.deliver(db, spa, row, sender)
    sender.assert_not_called()
    assert row.status == "pending" and row.delivery_status == "disabled"


@pytest.mark.asyncio
async def test_staff_access_requires_business_specific_assignment(data):
    from app.api.v1.booking_escalations import can_resolve, save_settings, Settings
    from app.models import User, UserRole
    from app.core.tenancy import TenantScope
    db, spa, owner, *_ = data
    staff = User(id=uuid.uuid4(), email="staff@example.test", hashed_password="test", role=UserRole.SPA_STAFF, tenant_id=spa.id, is_active=True)
    db.add(staff)
    await db.commit()
    scope = TenantScope.for_tenant(spa.id)
    assert not await can_resolve(staff, scope, db)
    await save_settings(Settings(staff_user_ids=[staff.id]), scope, db)
    assert await can_resolve(staff, scope, db)
    assert await can_resolve(owner, scope, db)
    with pytest.raises(HTTPException) as error:
        await save_settings(Settings(staff_user_ids=[uuid.uuid4()]), scope, db)
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_disabled_business_does_not_disable_another_business_alert(data):
    db, spa, *_ = data
    from app.models import SpaAccount
    other = SpaAccount(id=uuid.uuid4(), name="Other business", timezone="America/Los_Angeles")
    db.add(other)
    await db.commit()
    spa.notification_settings = {"booking_escalation": {"notifications_enabled": False}}
    first = await e.record(db, spa, "same-call", {}, TimeoutError())
    second = await e.record(db, other, "same-call", {}, TimeoutError())
    sender = AsyncMock(return_value={"status": "accepted"})
    await e.deliver(db, spa, first, sender)
    await e.deliver(db, other, second, sender)
    assert sender.await_count == 1 and sender.call_args.args[0].id == other.id


@pytest.mark.asyncio
async def test_cancelled_or_pending_booking_does_not_count_as_recovered():
    from app.services.square_booking_recovery import reconcile
    p = payload()
    for status in ("CANCELLED_BY_CUSTOMER", "PENDING"):
        record = {**p["booking"], "id": "old", "status": status}
        assert await reconcile(SimpleNamespace(_request=AsyncMock(return_value={"bookings": [record]})), p) is None
