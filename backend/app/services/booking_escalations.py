"""Persistent staff work queue. No customer notification is implied by an alert."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from app.models.booking_escalation import BookingEscalation
from app.services.secure_payment import redact_payment_text
from app.services.square_booking_recovery import classify

SQUARE_URL = "https://squareup.com/dashboard/appointments/calendar"


def now():
    return datetime.now(timezone.utc)


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return redact_payment_text(value)[:4000] if isinstance(value, str) else value


async def record(db, spa, call_reference, details, error):
    tenant_id = spa.id
    row = (await db.execute(select(BookingEscalation).where(
        BookingEscalation.tenant_id == spa.id, BookingEscalation.call_reference == call_reference))).scalar_one_or_none()
    if row:
        return row
    priority = "normal"
    try:
        start = datetime.fromisoformat(details["requested_start"].replace("Z", "+00:00"))
        if start.tzinfo and start.astimezone(ZoneInfo(spa.timezone)).date() == now().astimezone(ZoneInfo(spa.timezone)).date():
            priority = "urgent"
    except (KeyError, TypeError, ValueError):
        pass
    errors, cause = [], error
    for _ in range(4):
        if cause is None:
            break
        errors.append({"type": type(cause).__name__, "code": getattr(cause, "code", None), "message": str(cause)})
        cause = cause.__cause__ or cause.__context__
    row = BookingEscalation(tenant_id=spa.id, call_reference=call_reference, status="pending",
        priority=priority, category=classify(error), details=clean({**details,
            "provider_errors": errors,
            "error_code": getattr(error, "code", None), "error_details": str(error), "error_type": type(error).__name__,
            "timezone": spa.timezone, "manual_booking_url": SQUARE_URL if details.get("provider") == "square" else None,
            "provider_success_verified": bool(details.get("provider_success_verified"))}), delivery={}, history=[])
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        row = (await db.execute(select(BookingEscalation).where(
            BookingEscalation.tenant_id == tenant_id, BookingEscalation.call_reference == call_reference))).scalar_one()
    return row


async def unresolved_booking(db, tenant_id, phone, start):
    """A new call must not bypass an uncertain write from an earlier call."""
    if not tenant_id or not phone:
        return None
    rows = (await db.execute(select(BookingEscalation).where(
        BookingEscalation.tenant_id == tenant_id,
        BookingEscalation.status.in_(["pending", "contacted"]),
        BookingEscalation.details["customer_phone"].as_string() == phone,
        BookingEscalation.details["requested_start"].as_string() == start.astimezone(timezone.utc).isoformat(),
    ))).scalars()
    return next((row for row in rows if row.category in {"temporary", "unknown_outcome"}
                 or row.details.get("provider_success_verified")), None)


async def deliver(db, spa, row, sender=None):
    """Claim before I/O: uncertain delivery is visible and never blindly resent."""
    config = dict(getattr(spa, "notification_settings", None) or {})
    rules = config.get("booking_escalation") or {}
    if rules.get("notifications_enabled", True) is False:
        row.delivery_status, row.delivery = "disabled", {"status": "disabled"}
        await db.commit()
        return {"status": "disabled"}
    categories = rules.get("categories")
    if categories and row.category not in categories and row.priority != "urgent":
        row.delivery_status, row.delivery = "excluded_by_rule", {"status": "excluded_by_rule"}
        await db.commit()
        return {"status": "excluded_by_rule"}
    # One alert batch per request, including across workers and repeated tool calls.
    claimed = await db.execute(update(BookingEscalation).where(
        BookingEscalation.id == row.id, BookingEscalation.tenant_id == spa.id,
        BookingEscalation.version == row.version, BookingEscalation.delivery_status == "pending").values(
            delivery_status="sending_or_unknown", delivery={"status": "sending_or_unknown"}, version=BookingEscalation.version + 1))
    await db.commit()
    if not claimed.rowcount:
        await db.refresh(row)
        return row.delivery
    if sender is None:
        from app.services.escalation_mail import send_staff
        sender = send_staff
    summary = f"{'URGENT: ' if row.priority == 'urgent' else ''}Needs Staff Attention: booking request {row.id}. Review customer and appointment details in Cara. No appointment is confirmed."
    try:
        result = await sender(spa, "booking_failed", summary)
    except Exception:
        result = {"status": "delivery_unknown", "reason": "Inspect messaging provider before resending."}
    row.delivery_status = str(result.get("status", "unknown"))
    row.delivery = result
    await db.commit()
    return result


async def dispatch_pending(db):
    from app.models import SpaAccount
    rows = list((await db.execute(select(BookingEscalation).where(
        BookingEscalation.delivery_status == "pending").order_by(
            BookingEscalation.priority.desc(), BookingEscalation.created_at).limit(20))).scalars())
    for row in rows:
        spa = await db.get(SpaAccount, row.tenant_id)
        if spa:
            await deliver(db, spa, row)


async def run_dispatcher():
    """Durable polling outbox; never hold a live conversation for staff delivery."""
    import asyncio
    import logging
    from app.core.database import AsyncSessionLocal
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await dispatch_pending(db)
        except Exception:
            logging.getLogger(__name__).exception("Escalation outbox needs attention")
        await asyncio.sleep(10)


async def resolve(db, tenant_id, request_id, actor_id, status, expected_version, note, booking_id=None):
    row = (await db.execute(select(BookingEscalation).where(
        BookingEscalation.id == request_id, BookingEscalation.tenant_id == tenant_id).with_for_update())).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Request not found")
    if row.version != expected_version:
        raise HTTPException(409, "Request changed. Refresh before updating.")
    if status not in {"pending", "contacted", "booked", "unable_to_book"}:
        raise HTTPException(422, "Invalid request status")
    if status == "booked" and not booking_id:
        raise HTTPException(422, "Record the provider booking reference from manual scheduling.")
    row.history = [*row.history, {"at": now().isoformat(), "actor": str(actor_id),
        "from": row.status, "to": status, "note": clean(note), "provider_booking_id": booking_id}]
    row.status = status
    row.version += 1
    # Staff-reported booking is distinct from verified provider success.
    if booking_id:
        row.details = {**row.details, "staff_reported_booking_id": booking_id}
    await db.commit()
    return row


async def customer_followup(db, tenant_id, request_id, actor_id, consent_note, sender=None):
    """Opt-in assistance follow-up only; booking confirmations remain Square's job."""
    from app.models import Contact
    row = (await db.execute(select(BookingEscalation).where(
        BookingEscalation.id == request_id, BookingEscalation.tenant_id == tenant_id).with_for_update())).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Request not found")
    if row.status != "unable_to_book":
        raise HTTPException(409, "Use Square for booking confirmations. Cara follow-up is available for Unable to Book requests only.")
    if row.category in {"temporary", "unknown_outcome"} or row.details.get("provider_success_verified"):
        raise HTTPException(409, "Provider outcome is uncertain or a booking exists. Reconcile and communicate through the booking provider instead.")
    if not consent_note.strip():
        raise HTTPException(422, "Record the customer's explicit SMS follow-up consent.")
    if row.details.get("customer_followup"):
        return row.details["customer_followup"]
    contact = (await db.execute(select(Contact).where(Contact.tenant_id == tenant_id,
        Contact.phone_number == row.details.get("customer_phone")))).scalar_one_or_none()
    metadata = (contact.extra_metadata or {}) if contact else {}
    if not contact or metadata.get("sms_opt_out") or metadata.get("do_not_contact"):
        raise HTTPException(409, "Customer missing or opted out; do not send.")
    phone = contact.phone_number
    details = {**row.details, "customer_followup": {"status": "sending_or_unknown"}}
    claimed = await db.execute(update(BookingEscalation).where(BookingEscalation.id == row.id,
        BookingEscalation.tenant_id == tenant_id, BookingEscalation.version == row.version).values(
            details=details, version=BookingEscalation.version + 1))
    if not claimed.rowcount:
        await db.rollback()
        raise HTTPException(409, "Request changed. Refresh before sending.")
    row.history = [*row.history, {"at": now().isoformat(), "actor": str(actor_id),
        "action": "customer_followup_consent", "note": clean(consent_note)}]
    await db.commit()
    if sender is None:
        from app.services.twilio_service import twilio_service
        sender = twilio_service.send_sms
    try:
        sid = await sender(phone, "Following up on your appointment request: we couldn't complete that reservation. Please contact the business if you would like help choosing another appointment. No appointment was confirmed by this message.")
        result = {"status": "accepted" if sid else "failed_or_unknown"}
    except Exception:
        result = {"status": "delivery_unknown"}
    row.details = {**row.details, "customer_followup": result}
    await db.commit()
    return result
