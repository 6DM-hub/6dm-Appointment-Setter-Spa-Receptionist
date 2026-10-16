"""Persistent staff work queue. No customer notification is implied by an alert."""
import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
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
    return next((row for row in rows if row.category in {"temporary", "unknown_outcome", PENDING_CREATE}
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
    await promote_stale_change_fences(db)
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
    if (row.category in (UNRESOLVED_CHANGE, PENDING_CREATE) and row.delivery_status == "held"
            and status not in _OPEN_STATUSES):
        # Still being sent: its outcome is not known yet, so it cannot be
        # resolved (that would unblock the appointment mid-write).
        raise HTTPException(409, "This change is still being sent to the calendar. Try again shortly.")
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


UNRESOLVED_CHANGE = "unresolved_change"
_OPEN_STATUSES = ("pending", "contacted")


async def record_unresolved_change(db, tenant_id, call_reference, details):
    """Durably record a booking change whose provider outcome is unknown.

    Keyed by tenant and appointment, it blocks every later move or
    cancellation of that appointment -- from any call or the dashboard --
    until staff resolve it (status no longer pending/contacted).
    """
    if not tenant_id or not details.get("appointment_id"):
        return None
    reference = f"change:{details['appointment_id']}:{call_reference}"[:128]
    existing = (await db.execute(select(BookingEscalation).where(
        BookingEscalation.tenant_id == tenant_id,
        BookingEscalation.call_reference == reference))).scalar_one_or_none()
    if existing:
        return existing
    row_details = clean(dict(details))
    # Identifiers are not payment text: payment redaction can mistake a UUID's
    # digit run for a card number, and the block is looked up by these values.
    for key in ("appointment_id", "external_booking_id"):
        if details.get(key) is not None:
            row_details[key] = str(details[key])
    row = BookingEscalation(
        tenant_id=tenant_id, call_reference=reference, status="pending", priority="urgent",
        category=UNRESOLVED_CHANGE, details=row_details, delivery={}, history=[],
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
    return row


# ---------------------------------------------------------------------------
# Write-ahead change fence
# ---------------------------------------------------------------------------
# Before CARA sends a move or cancellation of an existing appointment to an
# external calendar, it durably records that the change is IN FLIGHT. The
# record is an ordinary unresolved-change row, so while it exists every later
# move or cancellation of that appointment -- any call, any worker, the
# dashboard -- is blocked. A verified outcome (provider confirmed it, or
# definitely refused it) removes the record; an unknown outcome turns it into
# a staff alert; a worker that dies mid-write leaves it in place, and the
# dispatcher alerts staff once it is stale. Nothing ever clears it silently.
FENCE_STALE_AFTER = timedelta(minutes=5)


def _change_details(details, **extra):
    row_details = clean({**dict(details), **extra})
    # Identifiers are not payment text (see record_unresolved_change).
    for key in ("appointment_id", "external_booking_id"):
        if details.get(key) is not None:
            row_details[key] = str(details[key])
    return row_details


@asynccontextmanager
async def _own_transaction(db):
    """A session on its OWN connection, so a fence commits without committing
    (or releasing the locks of) the caller's open transaction."""
    async with AsyncSession(bind=db.bind, expire_on_commit=False) as fence_db:
        yield fence_db


class ChangeInFlight(Exception):
    """Another change of this appointment is in flight or unresolved."""


async def open_change_fence(db, tenant_id, call_reference, details, *, fence_id=None):
    """Durably record a provider change before it is sent; returns its id.

    Exclusive per appointment: while one change is in flight or unresolved,
    opening another raises ChangeInFlight, so two requests (two calls, two
    dashboard users, a call and the dashboard) can never both send a change
    of the same appointment.
    """
    if not tenant_id or not details.get("appointment_id"):
        return None
    appointment_id = str(details["appointment_id"])
    reference = f"change:{appointment_id}:{uuid.uuid4().hex[:12]}:{call_reference}"[:128]
    async with _own_transaction(db) as fence_db:
        if fence_db.bind is not None and fence_db.bind.dialect.name == "postgresql":
            # Serialize the check-then-insert for this one appointment.
            await fence_db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"change-fence:{tenant_id}:{appointment_id}"},
            )
        if await unresolved_change_for(fence_db, tenant_id, appointment_id) is not None:
            raise ChangeInFlight(appointment_id)
        row = BookingEscalation(
            id=fence_id or uuid.uuid4(),   # known in advance, so a lost ack can be cleaned up
            tenant_id=tenant_id, call_reference=reference, status="pending", priority="urgent",
            category=UNRESOLVED_CHANGE,
            details=_change_details(details, stage="in_flight", opened_at=now().isoformat()),
            delivery={}, history=[], delivery_status="held",
        )
        fence_db.add(row)
        await fence_db.commit()
        return row.id


async def close_change_fence(db, fence_id, *, own_transaction=False):
    """The provider outcome is verified: the change is no longer in flight.

    In the caller's transaction (``own_transaction=False``) the removal commits
    atomically with the local row update; otherwise it commits on its own.
    """
    if not fence_id:
        return
    if not own_transaction:
        await db.execute(delete(BookingEscalation).where(BookingEscalation.id == fence_id))
        return
    async with _own_transaction(db) as fence_db:
        await fence_db.execute(delete(BookingEscalation).where(BookingEscalation.id == fence_id))
        await fence_db.commit()


async def mark_change_unknown(db, fence_id, details):
    """The provider outcome is unknown: keep the fence and alert staff."""
    if not fence_id:
        return None
    async with _own_transaction(db) as fence_db:
        row = await fence_db.get(BookingEscalation, fence_id)
        if row is None:
            return None
        row.details = {**(row.details or {}), **_change_details(details, stage="unknown_outcome")}
        row.delivery_status = "pending"
        row.status = "pending"   # an unknown outcome always (re)opens the block
        await fence_db.commit()
        return row


async def promote_stale_change_fences(db, *, older_than=FENCE_STALE_AFTER):
    """A change still in flight long after it was sent was interrupted: alert staff."""
    cutoff = now() - older_than
    rows = list((await db.execute(select(
        BookingEscalation.id, BookingEscalation.created_at, BookingEscalation.details,
    ).where(
        BookingEscalation.category.in_([UNRESOLVED_CHANGE, PENDING_CREATE]),
        BookingEscalation.delivery_status == "held",
    ))).all())
    changed = False
    for row_id, created, details in rows:
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created is not None and created > cutoff:
            continue
        # Conditional: a fence settled or marked unknown meanwhile is left alone.
        result = await db.execute(update(BookingEscalation).where(
            BookingEscalation.id == row_id, BookingEscalation.delivery_status == "held",
        ).values(details={**(details or {}), "stage": "interrupted"}, delivery_status="pending"))
        changed = changed or bool(result.rowcount)
    if changed:
        await db.commit()
    return changed


async def unresolved_change_for(db, tenant_id, appointment_id):
    """An unresolved change of this appointment, from any call, if one exists."""
    if not tenant_id or not appointment_id:
        return None
    rows = (await db.execute(select(BookingEscalation).where(
        BookingEscalation.tenant_id == tenant_id,
        BookingEscalation.category == UNRESOLVED_CHANGE,
        BookingEscalation.status.in_(list(_OPEN_STATUSES)),
    ))).scalars()
    return next((row for row in rows if str((row.details or {}).get("appointment_id")) == str(appointment_id)), None)


# ---------------------------------------------------------------------------
# Write-ahead pending create (Google)
# ---------------------------------------------------------------------------
# Before CARA asks Google to create an event it durably records the attempt:
# who it is for, the exact deterministic event id, the booking reference and
# the times. A verified create removes the record in the same transaction as
# the local appointment; a definite refusal removes it; an unclear outcome
# keeps it (staff are alerted through the booking escalation); a worker that
# dies mid-write leaves it "held" and the dispatcher alerts staff once stale.
# While it is open it blocks another attempt for the same caller and time, and
# explicit reconciliation can link the verified Google event to ONE local row.
PENDING_CREATE = "pending_create"


class CreateInFlight(Exception):
    """An earlier attempt for this booking intent is in flight or unresolved."""


def _create_reference(intent_key):
    return "create:" + hashlib.sha256(str(intent_key).encode("utf-8")).hexdigest()[:48]


def _create_details(details, **extra):
    row_details = clean({**dict(details), **extra})
    # Identifiers are looked up by value; payment redaction must not alter them.
    for key in ("booking_reference", "event_id", "calendar_id", "contact_id", "source_call_id",
                "user_id", "requested_start", "requested_end", "customer_phone", "contact_phone"):
        if details.get(key) is not None:
            row_details[key] = str(details[key])
    return row_details


async def pending_create_for(db, tenant_id, intent_key):
    """The open pending-create record of this booking intent, if any."""
    if not tenant_id or not intent_key:
        return None
    return (await db.execute(select(BookingEscalation).where(
        BookingEscalation.tenant_id == tenant_id,
        BookingEscalation.call_reference == _create_reference(intent_key),
    ))).scalar_one_or_none()


async def open_pending_create(db, tenant_id, intent_key, details, *, record_id=None):
    """Durably record a Google create before it is sent; returns its id.

    One record per (tenant, booking intent): a second attempt while one is in
    flight or unresolved raises CreateInFlight, so the same booking is never
    sent twice in parallel or after an unclear outcome.
    """
    if not tenant_id or not intent_key:
        return None
    reference = _create_reference(intent_key)
    async with _own_transaction(db) as create_db:
        if create_db.bind is not None and create_db.bind.dialect.name == "postgresql":
            await create_db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"pending-create:{tenant_id}:{reference}"},
            )
        if await pending_create_for(create_db, tenant_id, intent_key) is not None:
            raise CreateInFlight(reference)
        row = BookingEscalation(
            id=record_id or uuid.uuid4(),
            tenant_id=tenant_id, call_reference=reference, status="pending", priority="urgent",
            category=PENDING_CREATE,
            details=_create_details(details, stage="in_flight", opened_at=now().isoformat()),
            delivery={}, history=[], delivery_status="held",
        )
        create_db.add(row)
        try:
            await create_db.commit()
        except IntegrityError as exc:
            await create_db.rollback()
            raise CreateInFlight(reference) from exc
        return row.id


async def close_pending_create(db, record_id, *, own_transaction=False):
    """The create is verified, or definitely refused: remove the record.

    In the caller's transaction it is removed atomically with the local
    appointment insert; otherwise on its own connection.
    """
    if not record_id:
        return
    statement = delete(BookingEscalation).where(
        BookingEscalation.id == record_id, BookingEscalation.category == PENDING_CREATE)
    if not own_transaction:
        await db.execute(statement)
        return
    async with _own_transaction(db) as create_db:
        await create_db.execute(statement)
        await create_db.commit()


async def mark_pending_create_unresolved(db, record_id, details, *, escalated):
    """The outcome is unclear: keep the record open for reconciliation.

    ``escalated``: a booking escalation already alerts staff for this call, so
    this record is linked rather than alerting twice; otherwise it alerts.
    """
    if not record_id:
        return None
    async with _own_transaction(db) as create_db:
        row = await create_db.get(BookingEscalation, record_id)
        if row is None or row.category != PENDING_CREATE:
            return None
        row.details = {**(row.details or {}), **_create_details(details, stage="unknown_outcome")}
        row.delivery_status = "linked_to_escalation" if escalated else "pending"
        row.status = "pending"
        await create_db.commit()
        return row
