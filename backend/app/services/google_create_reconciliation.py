"""Explicit reconciliation of an unresolved Google create (G2).

Staff (a manager) invoke this for one open pending-create record, e.g. after
a lost reply or a worker restart. It only READS Google: one lookup of the
exact deterministic event id, accepted only on the same full proof as create
recovery (id, active status, start/end instants, tenant and signed CARA tag).

On proof it links that event to exactly ONE local appointment: the row is
created under the booking's intent key, whose unique index makes a concurrent
second reconciliation find the first row instead of inserting another. The
record is then marked booked in the same transaction. Anything short of proof
leaves the record open (blocked, staff alerted) and reports "unknown". It
never creates, moves or deletes anything in Google.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Appointment, AppointmentStatus, Contact
from app.models.booking_escalation import BookingEscalation
from app.models.google_calendar_connection import GoogleCalendarConnection
from app.services import google_calendar as gc
from app.services.booking_escalations import FENCE_STALE_AFTER, PENDING_CREATE, _OPEN_STATUSES, clean, now

logger = logging.getLogger(__name__)


def _parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


async def _linked_row(db, tenant_id, reference) -> Appointment | None:
    return (await db.execute(select(Appointment).where(
        Appointment.tenant_id == tenant_id, Appointment.booking_intent_key == reference,
    ))).scalar_one_or_none()


async def _durable_contact(db, tenant_id, details) -> Contact | None:
    """The caller's contact, from durable tenant-scoped data only.

    The recorded contact id is used only if that contact still exists for
    this tenant: a brand-new contact flushed in a booking transaction that
    later rolled back is gone. Then the tenant's contact for the recorded
    phone is reused, or recreated from the recorded identity. The unique
    (tenant, phone) index makes a concurrent recreation find the first row.
    """
    contact_id = _uuid(details.get("contact_id"))
    if contact_id is not None:
        found = (await db.execute(select(Contact).where(
            Contact.id == contact_id, Contact.tenant_id == tenant_id))).scalar_one_or_none()
        if found is not None:
            return found
    phone = details.get("contact_phone") or details.get("customer_phone")
    if not phone:
        return None

    async def by_phone():
        return (await db.execute(select(Contact).where(
            Contact.tenant_id == tenant_id, Contact.phone_number == phone))).scalar_one_or_none()

    existing = await by_phone()
    if existing is not None:
        return existing
    contact = Contact(tenant_id=tenant_id, phone_number=phone,
                      first_name=details.get("contact_first_name"),
                      last_name=details.get("contact_last_name"),
                      email=details.get("contact_email"))
    try:
        async with db.begin_nested():
            db.add(contact)
    except IntegrityError:
        return await by_phone()
    return contact


def _note(row: BookingEscalation, actor_id, action: str, **extra) -> None:
    row.history = [*(row.history or []), {"at": now().isoformat(), "actor": str(actor_id),
                                          "action": action, **clean(extra)}]


async def reconcile_pending_create(db, tenant_id, request_id, actor_id) -> dict:
    row = (await db.execute(select(BookingEscalation).where(
        BookingEscalation.id == request_id, BookingEscalation.tenant_id == tenant_id,
        BookingEscalation.category == PENDING_CREATE,
    ).with_for_update())).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Request not found")
    details = dict(row.details or {})
    status, delivery_status, created = row.status, row.delivery_status, row.created_at
    if status not in _OPEN_STATUSES:
        await db.rollback()
        return {"status": "already_resolved", "request_status": status,
                "appointment_id": details.get("reconciled_appointment_id")}
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if delivery_status == "held" and created is not None and created > now() - FENCE_STALE_AFTER:
        await db.rollback()
        raise HTTPException(409, "This booking is still being sent to the calendar. Try again shortly.")

    reference = details.get("booking_reference")
    start, end = _parse(details.get("requested_start")), _parse(details.get("requested_end"))
    connection = (await db.execute(select(GoogleCalendarConnection).where(
        GoogleCalendarConnection.spa_id == tenant_id))).scalar_one_or_none()
    reason = None
    if not reference or start is None or end is None or not (
            details.get("contact_id") or details.get("contact_phone") or details.get("customer_phone")):
        reason = "the record is missing booking identity"
    elif connection is None or connection.selected_calendar_id != details.get("calendar_id"):
        reason = "the business's Google calendar is no longer the one the booking was sent to"
    event_id = None
    if reason is None:
        try:
            event_id = await gc.verify_created_event(connection, reference=reference, start=start, end=end)
        except gc.GoogleCalendarError:
            reason = "Google did not prove the event"
    if event_id is None:
        _note(row, actor_id, "reconcile_unverified", reason=reason)
        row.version += 1
        await db.commit()
        return {"status": "unknown", "reason": reason}

    appointment = await _linked_row(db, tenant_id, reference)
    if appointment is None:
        contact = await _durable_contact(db, tenant_id, details)
        if contact is None:
            _note(row, actor_id, "reconcile_unverified", reason="the caller's identity could not be recovered")
            row.version += 1
            await db.commit()
            return {"status": "unknown", "reason": "the caller's identity could not be recovered"}
        appointment = Appointment(
            tenant_id=tenant_id, user_id=_uuid(details.get("user_id")), contact_id=contact.id,
            source_call_id=_uuid(details.get("source_call_id")),
            title=details.get("title") or "Appointment",
            description=details.get("description"),
            start_time=start, end_time=end, status=AppointmentStatus.SCHEDULED,
            booking_provider="google_calendar", external_booking_id=event_id,
            booking_intent_key=reference,
        )
        db.add(appointment)
        try:
            await db.flush()
        except IntegrityError:
            # A concurrent reconciliation (or the original worker) linked it
            # first: the unique intent key guarantees one row. Re-read.
            await db.rollback()
            appointment = await _linked_row(db, tenant_id, reference)
            if appointment is None:
                raise
            return {"status": "already_linked", "appointment_id": str(appointment.id)}
    elif appointment.external_booking_id != event_id:
        _note(row, actor_id, "reconcile_conflict", existing_appointment_id=str(appointment.id))
        row.version += 1
        await db.commit()
        return {"status": "unknown", "reason": "a different local booking already holds this intent"}
    row.status = "booked"
    row.version += 1
    row.details = {**details, "reconciled_event_id": event_id,
                   "reconciled_appointment_id": str(appointment.id),
                   "provider_success_verified": True}
    _note(row, actor_id, "reconcile_linked", appointment_id=str(appointment.id), event_id=event_id)
    await db.commit()
    logger.info("Pending Google create %s reconciled to appointment %s", row.id, appointment.id)
    return {"status": "booked", "appointment_id": str(appointment.id), "event_id": event_id}
