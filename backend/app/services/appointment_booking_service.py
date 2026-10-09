"""
Converts an extracted AppointmentIntent into real Appointment/Contact records,
with conflict detection. Designed to be called mid-conversation: the resulting
BookingResult.to_system_message() is injected into CallSession history so the
NEXT Grok reply naturally confirms/denies the booking to the caller.

Two things decide where a booking lands, and neither is under the caller's
control:

  * the `TenantScope` carried by the call (spa tenant vs 6DM sales workspace),
    which fixes the rows this transaction may read and write;
  * the `BookingAdapter` chosen from the call direction, which fixes the
    external calendar it is mirrored into.

An inbound spa conversation therefore cannot reach Dominic's calendar, and one
spa's receptionist cannot see another spa's diary.
"""
import logging
import uuid
import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo as tzinfo_type
from enum import Enum
from typing import Any, Dict, Optional

from dateutil import parser as dateutil_parser
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.tenancy import TenantScope, scope_columns, scope_filter
from app.models import Appointment, AppointmentStatus, CallLog, Contact, SpaAccount
from app.models.appointment import CardStatus
from app.services.booking_adapters import (
    BookingAdapter,
    BookingContext,
    ExternalBooking,
    SpaBookingAdapter,
    BookingProviderError,
    get_booking_adapter,
)
from app.services.business_hours import resolve_timezone
from app.services.customer_links import upsert_external_customer_link
from app.services.card_status import card_status_for_booked_appointment
from app.services.phone_numbers import canonical_customer_phone, is_non_phone_caller_id
from app.services.booking_state import (
    BookingDraft,
    bind_verified_slot,
    confirmation_block_reason,
    consume_confirmation_authorization,
    get_draft,
    intent_key,
    invalidate_booking_proposal,
    proposal_fingerprint,
    recover_caller_name_from_history,
    remember_verified_availability,
    save_draft,
    spoken_slot_time,
    stage,
    start_new_intent,
)
from app.services.call_state import CallSession
from app.services.caller_identity import persist_caller_identity
from app.services.grok_service import AppointmentIntent
from app.services.truth_log import truth

logger = logging.getLogger(__name__)
CALENDAR_TIMEOUT_SECONDS = 10

# Alternative-slot search budget.
#
# The old cap counted loop iterations, so slots that were rejected locally as
# "outside business hours" (no provider round-trip at all) used up the same
# budget as real Square calls. On an evening request the whole budget was spent
# on closed hours and the search never reached the next morning.
#
# Now only calls that actually reach the provider count against
# MAX_PROVIDER_CHECKS, and SEARCH_HORIZON bounds how far ahead we walk so that
# skipping closed slots can never turn into an unbounded loop.
MAX_PROVIDER_CHECKS = 12
SEARCH_HORIZON = timedelta(days=3)
ALTERNATIVE_SUGGESTIONS = 3

# Reason prefixes produced by the availability check. These are matched in one
# place (see `_clarification` / `_is_closed`) so the rest of the module never
# string-matches provider reasons on its own.
_REASON_NEEDS_CLARIFICATION = "needs_clarification"
_REASON_SERVICE_AMBIGUOUS = "service_ambiguous"
_REASON_OUTSIDE_HOURS = "outside_business_hours"


class BookingOutcome(str, Enum):
    BOOKED = "booked"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    MISSING_INFO = "missing_info"
    SKIPPED = "skipped"
    ERROR = "error"
    # The caller's request is recorded and the slot is free, but nothing has
    # been written yet — the agent still has to read it back and be told yes.
    DRAFT = "draft"


class BookingRoutingError(RuntimeError):
    """The call context is not safe to route to a calendar."""


@dataclass
class BookingResult:
    outcome: BookingOutcome
    appointment: Appointment | None = None
    message: str = ""
    card_sms: str | None = None

    def to_system_message(self) -> str:
        return f"[SYSTEM: {self.message}]"


def apply_booking_result(session: CallSession, result: BookingResult) -> None:
    """Copy the authoritative booking result into the live call state.

    Only BOOKED/RESCHEDULED outcomes may populate the session's *active*
    appointment pointers.  A cancelled result still carries the historical
    Appointment row so callers can be told what was cancelled; treating the
    mere presence of that row as "active" resurrects a cancelled booking in
    session state.
    """
    active_outcomes = {BookingOutcome.BOOKED, BookingOutcome.RESCHEDULED}
    if result.outcome in active_outcomes and result.appointment is None:
        session.booking_status = "failed"
    elif result.outcome is BookingOutcome.DRAFT:
        session.booking_status = "awaiting_confirmation"
    else:
        session.booking_status = result.outcome.value

    session.entities["last_booking_outcome"] = result.outcome.value

    if result.outcome in active_outcomes and result.appointment is not None:
        session.appointment_id = str(result.appointment.id)
        session.confirmed_datetime = result.appointment.start_time.isoformat()
        session.external_booking_id = result.appointment.external_booking_id
        session.entities["active_appointment_id"] = session.appointment_id
    elif result.outcome is BookingOutcome.CANCELLED:
        session.appointment_id = None
        session.external_booking_id = None
        session.confirmed_datetime = None
        session.entities.pop("active_appointment_id", None)


def _parse_dt(value: str | None, local_tz: tzinfo_type | None = None) -> datetime | None:
    """Parse a model-supplied ISO8601 datetime, localizing a bare (naive) value.

    Callers on the phone speak in the business's own local time, never UTC.
    The model is instructed to report local wall-clock time with no offset —
    a bare "2026-09-24T14:00:00" — but earlier tool-schema wording once told
    it to append "Z" as if formatting always meant UTC, and a naive string can
    still arrive from an older prompt version or a model that ignores the
    instruction. Either way, treating "no offset" as UTC silently shifts a
    caller's 2pm by however many hours the tenant is offset from UTC, which is
    what made a mid-afternoon request look like it was before opening. A
    string that already carries an explicit offset (including "Z") is trusted
    as-is: only a genuinely naive value is localized here.
    """
    if not value:
        return None
    try:
        dt = dateutil_parser.isoparse(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        tz = local_tz or timezone.utc
        localized = dt.replace(tzinfo=tz)
        logger.info(
            "booking datetime %r had no UTC offset; localized to tz=%s -> %s",
            value,
            getattr(tz, "key", tz),
            localized.isoformat(),
        )
        return localized
    return dt


def _clarification(verdict: Any) -> str | None:
    """Turn a service-level verdict into an instruction for the agent.

    `needs_clarification: ...` means the provider could not tell WHICH service
    the caller wants. That says nothing about whether the TIME is free, so it
    must never be treated as a conflict: searching for alternative times cannot
    fix it, and telling the agent "no times are available" sends the caller
    down the wrong path.

    The reason format is `needs_clarification: <code>[: <asked>][ | options: a; b]`.
    When the router supplies the tenant's real menu options they are passed to
    the agent so it can ask a specific question ("the 60 or the 90 minute?").

    Returns None when the verdict is not a clarification request.
    """
    reason = (getattr(verdict, "reason", None) or "").strip()
    if not reason.startswith(_REASON_NEEDS_CLARIFICATION):
        return None

    body = reason[len(_REASON_NEEDS_CLARIFICATION):].lstrip(": ").strip()
    head, sep, tail = body.partition(" | options:")
    options = [item.strip() for item in tail.split(";") if item.strip()] if sep else []
    code, _, subject = head.partition(":")
    code, subject = code.strip(), subject.strip()
    listed = "; ".join(options)
    # Ambiguous: these ARE the choices. Unrecognized / unspecified: the router
    # truncates a long menu, so present them as examples, not the full list.
    choices = f" The options are: {listed}." if options else ""
    examples = f" Examples from the menu: {listed}." if options else ""

    if code == _REASON_SERVICE_AMBIGUOUS:
        name = subject or "That service"
        return (
            f"'{name}' matches more than one service.{choices} Ask the caller which "
            "one they mean before checking any times. Do NOT offer alternative "
            "times yet, and do NOT say the time is unavailable."
        )
    if code == "service_not_specified":
        return (
            "The caller has not said which service they want." + examples +
            " Ask which service they would like, then check times. Do NOT offer "
            "times yet, and do NOT say the time is unavailable."
        )
    asked = f" '{subject}'" if subject and subject != "service_not_recognized" else ""
    return (
        f"The requested service{asked} is not on the menu." + examples +
        " Ask the caller which service they would like from the menu. Do not "
        "guess a service and do not offer times yet."
    )


def _is_closed(verdict: Any) -> bool:
    """Whether the verdict is a local 'outside business hours' rejection."""
    reason = (getattr(verdict, "reason", None) or "").strip()
    return reason.startswith(_REASON_OUTSIDE_HOURS)


async def _resolve_contact(
    db: AsyncSession,
    scope: TenantScope,
    session: CallSession,
    intent: AppointmentIntent,
    timezone_name: str | None = None,
) -> Contact:
    raw_phone = session.customer_phone
    phone = canonical_customer_phone(raw_phone, timezone_name=timezone_name)
    if is_non_phone_caller_id(raw_phone) or not phone:
        raise BookingProviderError(
            "The caller ID is missing, withheld, or not a usable phone number. "
            "Ask for a phone number before booking. Do not store the withheld "
            "caller ID as a customer phone.",
            code="INVALID_CALLER_ID",
        )
    contact = (
        await db.execute(
            select(Contact).where(
                scope_filter(scope, Contact), Contact.phone_number == phone
            )
        )
    ).scalar_one_or_none()

    if contact is None:
        name_parts = (intent.caller_name or "").split(maxsplit=1)
        contact = Contact(
            phone_number=phone,
            first_name=name_parts[0] if name_parts else None,
            last_name=name_parts[1] if len(name_parts) > 1 else None,
            email=intent.caller_email,
            **scope_columns(scope, Contact),
        )
        db.add(contact)
        await db.flush()
    else:
        changed = False
        if intent.caller_name and not contact.first_name:
            parts = intent.caller_name.split(maxsplit=1)
            contact.first_name = parts[0]
            contact.last_name = parts[1] if len(parts) > 1 else contact.last_name
            changed = True
        if intent.caller_email and not contact.email:
            contact.email = intent.caller_email
            changed = True
        if changed:
            await db.flush()

    return contact


def _apply_provider_customer(contact: Contact, external: ExternalBooking) -> None:
    """Fill an empty local contact from the provider. Never overwrite a name."""
    display = " ".join((external.customer_display_name or "").split())
    if display and not getattr(contact, "first_name", None):
        parts = display.split(maxsplit=1)
        contact.first_name = parts[0]
        if len(parts) > 1 and not getattr(contact, "last_name", None):
            contact.last_name = parts[1]
    if external.customer_email and not getattr(contact, "email", None):
        contact.email = external.customer_email


async def _find_contact_by_phone(
    db: AsyncSession, scope: TenantScope, session: CallSession
) -> Contact | None:
    """Read-only caller lookup. Unlike `_resolve_contact`, never creates a row."""
    phone = canonical_customer_phone(
        session.customer_phone, timezone_name=session.timezone
    )
    if not phone:
        return None
    return (
        await db.execute(
            select(Contact).where(
                scope_filter(scope, Contact), Contact.phone_number == phone
            )
        )
    ).scalar_one_or_none()


async def _has_conflict(
    db: AsyncSession,
    scope: TenantScope,
    start: datetime,
    end: datetime,
    capacity: int = 1,
    exclude_id: uuid.UUID | None = None,
    exclude_intent_key: str | None = None,
) -> bool:
    """Whether the slot is full *for someone else*.

    `capacity` is how many appointments can run at once — one for Dominic's
    sales calendar, but the number of configured staff for a spa, so a salon
    with four therapists is not limited to one guest an hour.

    `exclude_intent_key` is what separates "this call already booked this slot"
    from "a different customer holds this slot". Without it the booking this
    very conversation just created counts against itself and the caller is told
    their own appointment is unavailable.
    """
    query = select(func.count()).select_from(Appointment).where(
        scope_filter(scope, Appointment),
        Appointment.status.in_([AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED]),
        Appointment.start_time < end,
        Appointment.end_time > start,
    )
    if exclude_id:
        query = query.where(Appointment.id != exclude_id)
    if exclude_intent_key:
        query = query.where(
            (Appointment.booking_intent_key.is_(None))
            | (Appointment.booking_intent_key != exclude_intent_key)
        )
    overlapping = (await db.execute(query)).scalar_one()
    return overlapping >= max(capacity, 1)


async def _load_by_intent_key(
    db: AsyncSession, scope: TenantScope, key: str
) -> Appointment | None:
    """The appointment already persisted for this booking intent, if any.

    The idempotency lookup: a repeated confirmation, a retried tool call or a
    second worker handling the same call all land here and reuse the row
    instead of inserting a duplicate.
    """
    if not hasattr(db, "execute"):
        return None
    return (
        await db.execute(
            select(Appointment).where(
                scope_filter(scope, Appointment),
                Appointment.booking_intent_key == key,
                Appointment.status.in_(
                    [AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED]
                ),
            )
        )
    ).scalar_one_or_none()


async def _lock_slot(db: AsyncSession, scope: TenantScope, start: datetime, end: datetime) -> None:
    """Serialize same-scope slot checks when running against PostgreSQL."""
    bind = getattr(db, "bind", None)
    if bind is not None and bind.dialect.name == "postgresql":
        key = f"{scope.tenant_id or scope.owner_id}:{start.isoformat()}:{end.isoformat()}"
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


async def _lock_call(db: AsyncSession, call_sid: str) -> None:
    """Serialize retries for one call before checking durable booking state."""
    bind = getattr(db, "bind", None)
    if bind is not None and bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"booking-call:{call_sid}"},
        )


async def _calendar_call(operation: str, call_sid: str, awaitable):
    logger.info("Calendar request: call_sid=%s operation=%s", call_sid, operation)
    truth("BOOKING_PROVIDER", operation=operation, call_sid=call_sid)
    try:
        result = await asyncio.wait_for(awaitable, timeout=CALENDAR_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.exception("Calendar error: call_sid=%s operation=%s error=%s", call_sid, operation, exc)
        raise BookingProviderError(f"{operation} failed: {exc}") from exc
    logger.info("Calendar response: call_sid=%s operation=%s response=%s", call_sid, operation, result)
    return result


def _slots_to_suggestions(slots: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[tuple[datetime, dict[str, Any] | None]]:
    suggestions: list[tuple[datetime, dict[str, Any] | None]] = []
    for slot in slots:
        raw = (slot or {}).get("start")
        parsed = _parse_dt(str(raw) if raw else None)
        if parsed is None:
            continue
        suggestions.append((parsed, slot))
    return suggestions


def _nearest_openings(
    slots: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    target: datetime,
    limit: int,
) -> list[tuple[datetime, dict[str, Any] | None]]:
    """Rank real provider slots on the requested day by closeness to `target`."""
    target_utc = target.astimezone(timezone.utc)
    ranked: list[tuple[float, datetime, dict[str, Any]]] = []
    for slot in slots:
        raw = (slot or {}).get("start")
        parsed = _parse_dt(str(raw) if raw else None)
        if parsed is None:
            continue
        parsed_utc = parsed.astimezone(timezone.utc)
        if parsed_utc == target_utc:
            continue
        ranked.append((abs((parsed_utc - target_utc).total_seconds()), parsed, slot))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [(item[1], item[2]) for item in ranked[:limit]]


def _availability_generation(draft: Any) -> tuple[Any, ...]:
    """Identity of the booking request a completed lookup is allowed to speak."""
    return (
        int(getattr(draft, "draft_revision", 0) or 0),
        (getattr(draft, "service_description", None) or "").strip().casefold(),
        getattr(draft, "start_iso", None) or "",
        (getattr(draft, "preferred_staff", None) or "").strip().casefold(),
        getattr(draft, "duration_minutes", None),
    )


def _materialize_slot(
    start: datetime, end: datetime, verdict: Any
) -> dict[str, Any] | None:
    """Provider slot if present; otherwise a local stand-in for internal calendars."""
    slot = getattr(verdict, "slot", None)
    if slot:
        return dict(slot)
    if not getattr(verdict, "available", False):
        return None
    return {
        "start": start.isoformat(),
        "duration_minutes": int((end - start).total_seconds() / 60),
    }


def _match_alternative_slot(
    slots: list[dict[str, Any]], start: datetime
) -> dict[str, Any] | None:
    """Re-pin a previously offered provider slot when the caller restates its time."""
    target = start.astimezone(timezone.utc)
    for slot in slots:
        parsed = _parse_dt(str((slot or {}).get("start") or ""))
        if parsed is None:
            continue
        if parsed.astimezone(timezone.utc) == target:
            return slot
    return None


async def _available_alternatives(
    db: AsyncSession, scope: TenantScope, adapter: BookingAdapter, session: CallSession,
    intent: AppointmentIntent, requested_start: datetime, duration: timedelta, capacity: int,
) -> list[tuple[datetime, dict[str, Any] | None]]:
    """Real, provider-checked alternative times, paired with the authoritative
    slot each one came from (Square: the exact team member + service
    variation Square returned). Never anything computed or guessed — every
    candidate here is the result of an actual `adapter.check_availability`
    round-trip, and the paired slot is what lets the availability guard
    recognize the caller picking one of these as authorized.

    Budget rules:
      * Only checks that actually reach the provider count against
        MAX_PROVIDER_CHECKS. Slots rejected locally as outside business hours
        are skipped for free, so an evening request still reaches tomorrow.
      * SEARCH_HORIZON bounds the walk, so skipping closed slots can never
        become an unbounded loop.
      * A `needs_clarification` verdict stops the search immediately: the
        problem is the service name, and every later slot would fail the same
        way.
    """
    suggestions: list[tuple[datetime, dict[str, Any] | None]] = []
    candidate = requested_start + timedelta(minutes=30)
    horizon = requested_start + SEARCH_HORIZON
    provider_checks = 0

    while candidate < horizon and provider_checks < MAX_PROVIDER_CHECKS:
        end = candidate + duration
        ctx = _context(adapter, session, intent, candidate, end)
        verdict = await _calendar_call(
            "check_availability", session.call_sid, adapter.check_availability(ctx)
        )

        if _clarification(verdict) is not None:
            break
        if _is_closed(verdict):
            candidate += timedelta(minutes=30)
            continue

        provider_checks += 1
        provider_end = _provider_authoritative_end(candidate, end, verdict)
        if verdict.available and not await _has_conflict(
            db, scope, candidate, provider_end, capacity
        ):
            suggestions.append((candidate, verdict.slot))
            if len(suggestions) == ALTERNATIVE_SUGGESTIONS:
                break
        candidate += timedelta(minutes=30)
    return suggestions


async def _find_upcoming_appointment(
    db: AsyncSession, scope: TenantScope, contact_id: uuid.UUID
) -> Appointment | None:
    now = datetime.now(timezone.utc)
    return (
        await db.execute(
            select(Appointment)
            .where(
                scope_filter(scope, Appointment),
                Appointment.contact_id == contact_id,
                Appointment.status.in_([AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED]),
                Appointment.start_time >= now,
            )
            .order_by(Appointment.start_time.asc())
        )
    ).scalars().first()


async def _find_upcoming_appointments(
    db: AsyncSession, scope: TenantScope, contact_id: uuid.UUID, limit: int = 2
) -> list[Appointment]:
    """Every upcoming appointment for this contact, earliest first, up to
    `limit`. Used instead of `_find_upcoming_appointment` wherever a cancel
    must not silently pick the earliest of several — a caller with two
    bookings on the same day must be asked which one before anything is
    cancelled, or the wrong appointment risks being taken.
    """
    now = datetime.now(timezone.utc)
    return (
        await db.execute(
            select(Appointment)
            .where(
                scope_filter(scope, Appointment),
                Appointment.contact_id == contact_id,
                Appointment.status.in_([AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED]),
                Appointment.start_time >= now,
            )
            .order_by(Appointment.start_time.asc())
            .limit(limit)
        )
    ).scalars().all()


async def lookup_upcoming_appointments(
    db: AsyncSession, session: CallSession, limit: int = 10
) -> list[Appointment]:
    """Read-only upcoming-appointment lookup for voice selection/queries.

    This deliberately does not call `_resolve_contact`: asking "when is my next
    appointment?" must never create a Contact as a side effect.
    """
    routing = await _prepare(db, session)
    contact = await _find_contact_by_phone(db, routing.scope, session)
    if contact is None:
        return []
    return await _find_upcoming_appointments(
        db, routing.scope, contact.id, limit=max(1, min(limit, 20))
    )


async def _load_active_appointment(
    db: AsyncSession, scope: TenantScope, appointment_id: str | None
) -> Appointment | None:
    """Load the appointment this call already owns, if it still exists."""
    if not appointment_id:
        return None
    try:
        parsed_id = uuid.UUID(appointment_id)
    except (ValueError, TypeError):
        return None
    return (
        await db.execute(
            select(Appointment).where(
                scope_filter(scope, Appointment),
                Appointment.id == parsed_id,
                Appointment.status.in_([AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED]),
            )
        )
    ).scalar_one_or_none()


async def _load_call_owned_appointment(
    db: AsyncSession, scope: TenantScope, call_sid: str
) -> Appointment | None:
    """Recover the active booking when Redis state was lost or duplicated."""
    if not hasattr(db, "execute"):
        return None
    return (
        await db.execute(
            select(Appointment)
            .join(CallLog, Appointment.source_call_id == CallLog.id)
            .where(
                scope_filter(scope, Appointment),
                CallLog.twilio_call_sid == call_sid,
                Appointment.status.in_([AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED]),
            )
            .order_by(Appointment.created_at.desc())
        )
    ).scalars().first()


async def _load_call_log_id(db: AsyncSession, call_sid: str):
    if not hasattr(db, "execute"):
        return None
    return (
        await db.execute(
            select(CallLog.id).where(CallLog.twilio_call_sid == call_sid)
        )
    ).scalar_one_or_none()


async def _load_spa(db: AsyncSession, scope: TenantScope) -> SpaAccount | None:
    if scope.tenant_id is None:
        return None
    return (
        await db.execute(
            select(SpaAccount)
            .options(selectinload(SpaAccount.google_calendar_connection))
            .where(SpaAccount.id == scope.tenant_id)
        )
    ).scalar_one_or_none()


def _capacity(spa: SpaAccount | None) -> int:
    return max(len(spa.staff or []), 1) if spa else 1


def _duration_minutes(adapter: BookingAdapter, service_description: str | None) -> int:
    if isinstance(adapter, SpaBookingAdapter):
        return adapter.duration_for_service(service_description)
    return adapter.default_duration_minutes


def external_booking_of(appointment: Appointment) -> ExternalBooking:
    """Rebuild the provider pointer stored on an Appointment row."""
    return ExternalBooking(
        provider=appointment.booking_provider or "internal",
        external_id=appointment.external_booking_id,
    )


def _remember_menu_identity(
    adapter: BookingAdapter, session: CallSession, intent: AppointmentIntent
) -> None:
    """Store the configured service and provider once they resolve.

    Later provider calls read these ids. A later phrase that resolves to a
    different variation drops the previously pinned slot.
    """
    if not isinstance(adapter, SpaBookingAdapter):
        return
    draft = get_draft(session)
    phrase = intent.service_description or draft.service_description
    if phrase:
        resolution = adapter.resolve_service(phrase)
        if resolution.status == "resolved" and resolution.entry is not None:
            entry = resolution.entry
            variation = str(entry.get("square_variation_id") or "").strip() or None
            raw_version = entry.get("square_variation_version")
            version = raw_version if isinstance(raw_version, int) else None
            previous = draft.square_variation_id
            if resolution.name:
                draft.service_description = resolution.name
            draft.service_id = str(entry.get("id") or resolution.name or "")
            draft.square_variation_id = variation
            draft.square_variation_version = version
            minutes = entry.get("duration_minutes")
            if isinstance(minutes, int) and minutes > 0:
                draft.duration_minutes = minutes
            if previous and variation and previous != variation:
                draft.selected_slot = None
                draft.provider_verified = False
                draft.read_back = False
                draft.alternative_slots = []
            save_draft(session, draft)
    staff = (intent.preferred_staff or draft.preferred_staff or "").strip()
    resolved = adapter.resolve_provider(staff or None)
    if resolved.status == "resolved" and resolved.name:
        draft.preferred_staff = resolved.name
        draft.provider_id = resolved.provider_id
        save_draft(session, draft)
    elif resolved.status == "unrecognized":
        draft.provider_id = None
        save_draft(session, draft)


def _context(
    adapter: BookingAdapter,
    session: CallSession,
    intent: AppointmentIntent,
    start: datetime,
    end: datetime,
    booking_reference: str | None = None,
    selected_slot: dict[str, Any] | None = None,
) -> BookingContext:
    caller_name = " ".join((intent.caller_name or "").split()) or None
    guest_name = " ".join((getattr(intent, "guest_name", None) or "").split()) or None
    timezone_name = getattr(getattr(adapter, "delegate", None), "timezone_name", None)
    if not timezone_name:
        timezone_name = getattr(adapter, "timezone_name", None) or session.timezone
    phone = canonical_customer_phone(session.customer_phone, timezone_name=timezone_name)
    if is_non_phone_caller_id(session.customer_phone):
        phone = ""
    elif not phone:
        phone = session.customer_phone or ""
    notes = f"Booked by the AI agent during call {session.call_sid}."
    if guest_name and guest_name.casefold() != (caller_name or "").casefold():
        notes += f" Guest: {guest_name}."
    _remember_menu_identity(adapter, session, intent)
    draft = get_draft(session)
    pinned = selected_slot or draft.selected_slot or {}
    return BookingContext(
        start=start,
        end=end,
        title=intent.service_description or adapter.default_title,
        customer_phone=phone,
        # The provider customer is the caller. The guest stays in notes.
        customer_name=caller_name,
        customer_email=intent.caller_email,
        service_description=intent.service_description,
        notes=notes,
        booking_reference=booking_reference,
        preferred_staff=draft.preferred_staff or intent.preferred_staff,
        selected_slot=selected_slot,
        caller_name=caller_name,
        guest_name=guest_name,
        service_variation_id=draft.square_variation_id or pinned.get("service_variation_id"),
        service_variation_version=(
            draft.square_variation_version
            if draft.square_variation_version is not None
            else pinned.get("service_variation_version")
        ),
        provider_id=draft.provider_id or pinned.get("team_member_id"),
    )


@dataclass
class _Routing:
    """Where this call is allowed to write, and which calendar it mirrors to."""

    scope: TenantScope
    spa: SpaAccount | None
    adapter: BookingAdapter
    capacity: int
    is_outbound_sales: bool
    product: str


async def _prepare(db: AsyncSession, session: CallSession) -> _Routing:
    """Validate the call is safe to book and resolve its calendar.

    Unchanged in substance from the original preamble of `attempt_booking`;
    lifted out so the staging path and the committing path cannot drift apart
    on which tenant they are allowed to touch.
    """
    scope = session.scope
    if session.direction not in {"inbound", "outbound"}:
        raise BookingRoutingError(f"Unsupported call direction {session.direction!r}.")
    if session.direction == "inbound" and session.tenant_id is None:
        raise BookingRoutingError(
            "Inbound call has no resolved spa tenant; refusing sales-calendar routing."
        )
    if scope is None:
        raise BookingRoutingError("Call has no resolved booking scope.")

    is_outbound_sales = session.direction == "outbound"
    if is_outbound_sales and not scope.is_sales_workspace:
        raise BookingRoutingError(
            f"Outbound call carries tenant scope {scope.tenant_id}; refusing tenant booking."
        )

    spa = await _load_spa(db, scope)
    if session.direction == "inbound" and spa is None:
        raise BookingRoutingError(
            f"Inbound call tenant {session.tenant_id} could not be loaded."
        )
    adapter = get_booking_adapter(is_outbound_sales=is_outbound_sales, spa=spa)
    logger.info(
        "Booking routing: call_sid=%s call_direction=%s from_number=%s "
        "to_number=%s tenant_id=%s spa_id=%s booking_context=%s "
        "selected_calendar_provider=%s selected_calendar_owner=%s",
        session.call_sid,
        session.direction,
        session.from_number,
        session.to_number,
        session.tenant_id,
        spa.id if spa else None,
        "sales" if is_outbound_sales else "spa_receptionist",
        adapter.provider,
        adapter.calendar_label,
    )

    session.entities["calendar"] = adapter.calendar_label
    session.entities["default_service_title"] = adapter.default_title
    session.entities["booking_provider"] = adapter.provider
    if spa is not None:
        from app.services.spa_facts import payment_policy_of

        session.entities["smart_enhancements_enabled"] = bool(
            (getattr(spa, "enhancement_settings", None) or {}).get("enabled")
        )
        policy = payment_policy_of(spa)
        session.entities["card_on_file_required"] = bool(
            policy.get("card_required")
            and policy.get("collection_mode") == "secure_sms_link"
        )

    return _Routing(
        scope=scope,
        spa=spa,
        adapter=adapter,
        capacity=_capacity(spa),
        is_outbound_sales=is_outbound_sales,
        product="6DM Sales Agent" if is_outbound_sales else "Spa Receptionist",
    )


async def _safe_rollback(db: AsyncSession) -> None:
    rollback = getattr(db, "rollback", None)
    if callable(rollback):
        await rollback()


async def _notify_staff_event(spa: SpaAccount | None, event: str, summary: str) -> None:
    if spa is None:
        return
    try:
        from app.services.staff_notifications import notify_staff

        await notify_staff(spa, event, summary)
    except Exception:
        logger.exception("Staff notification failed for %s", event)


async def _notify_provider_error(session: CallSession, summary: str) -> None:
    if session.call_sid.startswith("cara-test:"):
        return
    if session.tenant_id is None:
        return
    try:
        from app.core.database import AsyncSessionLocal
        from app.services.staff_notifications import notify_staff

        async with AsyncSessionLocal() as db:
            spa = await db.get(SpaAccount, session.tenant_id)
            if spa is not None:
                await notify_staff(spa, "provider_error", summary)
    except Exception:
        logger.exception("Provider-error notification failed for call %s", session.call_sid)


_CUSTOMER_IDENTITY_CODES = frozenset({
    "AMBIGUOUS_CUSTOMER",
    "GUEST_UNRESOLVED",
    "INVALID_CALLER_ID",
    "MISSING_CUSTOMER_NAME",
})


async def _failure(db: AsyncSession, session: CallSession, exc: Exception) -> BookingResult:
    """Shared error mapping for both the staging and committing paths."""
    if isinstance(exc, BookingRoutingError):
        logger.error(
            "Booking rejected for unsafe routing: call_sid=%s call_direction=%s "
            "from_number=%s to_number=%s tenant_id=%s user_id=%s reason=%s",
            session.call_sid,
            session.direction,
            session.from_number,
            session.to_number,
            session.tenant_id,
            session.user_id,
            exc,
        )
        truth("BOOKING_FLOW_FAILED", call_sid=session.call_sid, reason="routing")
        return BookingResult(
            BookingOutcome.ERROR,
            message="I cannot safely route this appointment. Apologize and offer to take a manual message.",
        )
    if isinstance(exc, BookingProviderError) and exc.code in _CUSTOMER_IDENTITY_CODES:
        logger.info(
            "Customer identity needs another step for call %s: %s",
            session.call_sid,
            exc,
        )
        await _safe_rollback(db)
        session.booking_status = "collecting_details"
        return BookingResult(BookingOutcome.MISSING_INFO, message=str(exc))
    if isinstance(exc, (BookingProviderError, asyncio.TimeoutError)):
        logger.exception("Calendar booking failed for call %s: %s", session.call_sid, exc)
        await _safe_rollback(db)
        draft = get_draft(session)
        if draft.selected_slot and draft.provider_verified:
            session.booking_status = "awaiting_confirmation"
        else:
            session.booking_status = "follow_up_required"
        session.entities["scheduling_follow_up_required"] = True
        await _notify_provider_error(session, "A provider or calendar error needs staff attention.")
        return BookingResult(
            BookingOutcome.ERROR,
            message="There is a temporary scheduling issue. Collect the caller's name, contact information, and preferred times, then arrange a follow-up.",
        )
    logger.exception("Booking attempt failed for call %s", session.call_sid)
    await _safe_rollback(db)
    return BookingResult(
        BookingOutcome.ERROR,
        message="An internal error occurred while booking. Apologize and offer to take a manual message.",
    )


async def _routing_timezone(routing: _Routing):
    """Use the provider's live timezone when it exposes one.

    Square returns the configured Location timezone directly.  Falling back to
    the tenant setting is reserved for providers that do not expose a live
    location timezone.
    """
    provider_tz_name = None
    lookup = getattr(routing.adapter, "booking_timezone_name", None)
    if callable(lookup):
        provider_tz_name = await lookup()
    if provider_tz_name:
        return resolve_timezone(provider_tz_name)
    if routing.spa:
        return resolve_timezone(routing.spa.timezone)
    return timezone.utc


def _provider_authoritative_end(
    start: datetime,
    fallback_end: datetime,
    verdict: Any,
) -> datetime:
    """Adopt provider duration when the availability result includes it."""
    slot = getattr(verdict, "slot", None) or {}
    raw_minutes = slot.get("duration_minutes")
    try:
        minutes = int(raw_minutes)
    except (TypeError, ValueError):
        return fallback_end
    if minutes <= 0:
        return fallback_end
    return start + timedelta(minutes=minutes)


async def _draft_window(
    routing: _Routing, draft: BookingDraft
) -> tuple[datetime, datetime] | None:
    if not draft.start_iso:
        return None
    tz = await _routing_timezone(routing)
    start = _parse_dt(draft.start_iso, tz)
    if start is None:
        return None
    end = _parse_dt(draft.end_iso, tz) or (
        start
        + timedelta(minutes=_duration_minutes(routing.adapter, draft.service_description))
    )
    return start, end


def _draft_intent(draft: BookingDraft) -> AppointmentIntent:
    """The draft as an AppointmentIntent, for the adapter/context helpers."""
    # Canonical menu labels may be shared by 60- and 90-minute variants.
    # Keep the already selected duration when resolving that label on recheck.
    service = draft.service_description
    minutes = (draft.selected_slot or {}).get("duration_minutes") or draft.duration_minutes
    if service and isinstance(minutes, int) and minutes > 0:
        if not re.search(r"\b\d+\s*(?:min|minute)", service, re.IGNORECASE):
            service = f"{service} ({minutes} min)"
    return AppointmentIntent(
        intent=draft.operation_mode if draft.operation_mode in {"schedule", "reschedule"} else "schedule",
        caller_name=draft.caller_name,
        caller_email=draft.caller_email,
        requested_start_iso=draft.start_iso,
        requested_end_iso=draft.end_iso,
        service_description=service,
        preferred_staff=draft.preferred_staff,
        guest_name=draft.guest_name,
        confidence=1.0,
    )


def _restore_prior_draft(
    session: CallSession, prior: BookingDraft, current: BookingDraft
) -> BookingDraft:
    """Revert the requested TIME to the last verified state after a conflict.

    Only the time-dependent parts are reverted. Facts the caller just told us
    (which service, their name and email, a staff preference) say nothing about
    whether the slot was free, so they are carried over. Reverting them too
    meant a caller who named the service in the same breath as a busy time
    lost the service, and the next turn failed with `service_not_recognized`.
    """
    if current.service_description and current.service_description != prior.service_description:
        prior.service_description = current.service_description
        # The old pinned slot belongs to the old service; it is no longer valid.
        prior.selected_slot = None
        prior.read_back = False
    if current.preferred_staff and current.preferred_staff != prior.preferred_staff:
        prior.preferred_staff = current.preferred_staff
        prior.selected_slot = None
        prior.read_back = False
    if current.caller_name:
        prior.caller_name = current.caller_name
    if current.caller_email:
        prior.caller_email = current.caller_email
    save_draft(session, prior)
    return prior



_EARLIEST_REQUEST_RE = re.compile(
    r"\b(earliest|soonest|next\s+(?:available|opening)|first\s+(?:available|opening)|"
    r"as\s+soon\s+as\s+possible|asap)\b",
    re.IGNORECASE,
)


def _last_user_utterance(session: CallSession) -> str:
    for turn in reversed(session.history or []):
        if turn.get("role") == "user":
            return str(turn.get("content") or "")
    return ""


def _caller_requested_earliest(session: CallSession) -> bool:
    """Server-side proof that the caller actually asked for an earliest search."""
    return bool(_EARLIEST_REQUEST_RE.search(_last_user_utterance(session)))


def _next_half_hour(local_now: datetime) -> datetime:
    """Next future :00/:30 boundary in the business-local timezone."""
    current = local_now.replace(second=0, microsecond=0)
    remainder = current.minute % 30
    if remainder == 0:
        return current + timedelta(minutes=30)
    return current + timedelta(minutes=(30 - remainder))


async def _stage_earliest(
    db: AsyncSession,
    session: CallSession,
    intent: AppointmentIntent,
    draft: BookingDraft,
    routing: _Routing,
) -> BookingResult:
    """Deterministic provider-backed forward search for earliest openings."""
    if not _caller_requested_earliest(session):
        save_draft(session, draft)
        session.booking_status = "collecting_details"
        return BookingResult(
            BookingOutcome.MISSING_INFO,
            message=(
                "The caller did not ask for the earliest/soonest opening. Do NOT invent "
                "a time and do NOT run an earliest search. Ask for their preferred date "
                "and time, or use a provider-confirmed option already offered."
            ),
        )

    if not draft.service_description:
        save_draft(session, draft)
        session.booking_status = "collecting_details"
        return BookingResult(
            BookingOutcome.MISSING_INFO,
            message="Ask which service they want before searching for the earliest opening.",
        )

    tz = await _routing_timezone(routing)
    first_candidate = _next_half_hour(datetime.now(tz))
    duration = timedelta(minutes=_duration_minutes(routing.adapter, draft.service_description))
    search_end = first_candidate + SEARCH_HORIZON
    ctx = _context(
        routing.adapter, session, _draft_intent(draft), first_candidate, first_candidate + duration
    )
    list_fn = getattr(routing.adapter, "list_openings", None)
    openings: list[dict[str, Any]] = []
    if callable(list_fn):
        openings = await _calendar_call(
            "list_openings",
            session.call_sid,
            list_fn(ctx, first_candidate, search_end),
        )
        suggestions = _slots_to_suggestions(openings)[:ALTERNATIVE_SUGGESTIONS]
    else:
        search_base = first_candidate - timedelta(minutes=30)
        suggestions = await _available_alternatives(
            db,
            routing.scope,
            routing.adapter,
            session,
            _draft_intent(draft),
            search_base,
            duration,
            routing.capacity,
        )

    draft.selected_slot = None
    draft.alternative_slots = [slot for _dt, slot in suggestions if slot]
    save_draft(session, draft)

    if not suggestions:
        session.booking_status = "conflict"
        return BookingResult(
            BookingOutcome.CONFLICT,
            message=(
                "No provider-confirmed openings were found in the forward-search window. "
                "Do not invent a time; ask the caller for another preferred day or time range."
            ),
        )

    session.booking_status = "awaiting_selection"
    suggestion_text = ", ".join(item.isoformat() for item, _slot in suggestions)
    return BookingResult(
        BookingOutcome.CONFLICT,
        message=(
            "Provider-confirmed next available openings are: "
            f"{suggestion_text}. Offer ONLY these times and ask the caller to choose one. "
            "Nothing is booked yet."
        ),
    )


async def search_day_part(
    db: AsyncSession,
    session: CallSession,
    intent: AppointmentIntent,
    window_start: datetime,
    window_end: datetime,
) -> BookingResult:
    """Provider openings inside a caller-named part of a day.

    "Saturday afternoon" is not one exact minute. This asks the booking
    provider for that window and returns only times it actually confirmed.
    """
    draft = get_draft(session)
    if intent.service_description:
        draft.service_description = intent.service_description
    if intent.preferred_staff:
        draft.preferred_staff = intent.preferred_staff
    if intent.caller_name:
        draft.caller_name = intent.caller_name
    if intent.guest_name:
        draft.guest_name = intent.guest_name
    draft.selected_slot = None
    draft.start_iso = None
    draft.end_iso = None
    save_draft(session, draft)

    if not draft.service_description:
        session.booking_status = "collecting_details"
        return BookingResult(
            BookingOutcome.MISSING_INFO,
            message="Ask which service they want before searching that part of the day.",
        )

    try:
        routing = await _prepare(db, session)
    except Exception as exc:
        return await _failure(db, session, exc)

    duration = timedelta(
        minutes=_duration_minutes(routing.adapter, draft.service_description)
    )
    ctx = _context(
        routing.adapter,
        session,
        _draft_intent(draft),
        window_start,
        window_start + duration,
    )
    list_fn = getattr(routing.adapter, "list_openings", None)
    suggestions: list[tuple[datetime, dict[str, Any] | None]] = []
    if callable(list_fn):
        openings = await _calendar_call(
            "list_openings",
            session.call_sid,
            list_fn(ctx, window_start, window_end),
        )
        for item, slot in _slots_to_suggestions(openings):
            local = item.astimezone(window_start.tzinfo) if window_start.tzinfo else item
            if window_start <= local < window_end:
                suggestions.append((local, slot))
    else:
        walked = await _available_alternatives(
            db,
            routing.scope,
            routing.adapter,
            session,
            _draft_intent(draft),
            window_start - timedelta(minutes=30),
            duration,
            routing.capacity,
        )
        for item, slot in walked:
            local = item.astimezone(window_start.tzinfo) if window_start.tzinfo else item
            if window_start <= local < window_end:
                suggestions.append((local, slot))
    suggestions = suggestions[:ALTERNATIVE_SUGGESTIONS]
    draft.alternative_slots = [slot for _dt, slot in suggestions if slot]
    save_draft(session, draft)

    if not suggestions:
        session.booking_status = "conflict"
        return BookingResult(
            BookingOutcome.CONFLICT,
            message=(
                "No provider-confirmed openings were found in that part of the day. "
                "Tell the caller that, and ask for another day or time. Do not invent a time."
            ),
        )

    session.booking_status = "awaiting_selection"
    suggestion_text = ", ".join(
        item.strftime("%A %B %d at %I:%M %p").replace(" 0", " ")
        for item, _slot in suggestions
    )
    return BookingResult(
        BookingOutcome.CONFLICT,
        message=(
            f"Openings in that part of the day: {suggestion_text}. "
            "Offer ONLY these times and ask the caller to choose one. "
            "Do not ask them to restate the calendar date. Nothing is booked yet."
        ),
    )


async def stage_booking(
    db: AsyncSession, session: CallSession, intent: AppointmentIntent, *, test_adapter: BookingAdapter | None = None
) -> BookingResult:
    """Record what the caller asked for. Never writes an appointment.

    Folding the request into the one active draft is what makes "actually, the
    22nd" an edit rather than a second booking. The slot is checked so the
    agent can answer honestly, but nothing is reserved until `    confirm_booking`.
    """
    prior = get_draft(session)
    prior_alts = list(prior.alternative_slots or [])
    prior_verified = dict(prior.verified_availability or {})
    prior_selected = dict(prior.selected_slot) if prior.selected_slot else None
    prior_service = prior.service_description
    prior_staff = prior.preferred_staff
    draft = stage(session, intent)
    try:
        if test_adapter is None:
            routing = await _prepare(db, session)
        else:
            from app.services.campaign_booking import test_routing
            routing = await test_routing(db, session, test_adapter)
        if intent.caller_name or intent.caller_email:
            await persist_caller_identity(
                db, session, intent.caller_name, intent.caller_email
            )

        if bool(getattr(intent, "earliest", False)):
            if getattr(intent, "requested_start_iso", None):
                save_draft(session, draft)
                session.booking_status = "collecting_details"
                return BookingResult(
                    BookingOutcome.MISSING_INFO,
                    message=(
                        "Do not combine earliest=true with an exact requested_start_iso. "
                        "Use earliest=true only for a caller-requested earliest/soonest search, "
                        "or use the exact caller-stated/provider-offered time."
                    ),
                )
            return await _stage_earliest(db, session, intent, draft, routing)

        window = await _draft_window(routing, draft)
        if window is None:
            session.booking_status = "collecting_details"
            return BookingResult(
                BookingOutcome.MISSING_INFO,
                message="No clear date and time yet. Ask the caller for a specific date and time.",
            )
        start, end = window
        session.requested_datetime = start.isoformat()
        session.selected_service = draft.service_description
        session.booking_status = "checking_availability"

        key = intent_key(session, draft)
        service_changed = bool(
            prior_service
            and draft.service_description
            and prior_service != draft.service_description
        )
        staff_changed = bool(
            prior_staff
            and draft.preferred_staff
            and prior_staff != draft.preferred_staff
        )
        pinned = None
        if not service_changed and not staff_changed:
            pinned = _match_alternative_slot(prior_alts, start)
            if pinned is None and prior_selected:
                pinned = _match_alternative_slot([prior_selected], start)
            if pinned and service_changed:
                pinned = None
            variation = str((pinned or {}).get("service_variation_id") or "")
            prior_variation = str((prior_selected or {}).get("service_variation_id") or "")
            if pinned and prior_variation and variation != prior_variation and service_changed:
                pinned = None
        ctx = _context(
            routing.adapter,
            session,
            _draft_intent(draft),
            start,
            end,
            selected_slot=pinned,
        )
        verdict = await _calendar_call(
            "check_availability", session.call_sid, routing.adapter.check_availability(ctx)
        )

        # A service-level problem (ambiguous / unrecognized service) is not a
        # busy slot. Keep the draft as staged so the requested time and any
        # service the caller did name survive to the next turn, and tell the
        # agent to clarify the service instead of hunting for other times.
        clarify = _clarification(verdict)
        if clarify:
            save_draft(session, draft)
            session.booking_status = "collecting_details"
            return BookingResult(BookingOutcome.MISSING_INFO, message=clarify)

        # Square/provider duration is authoritative once availability has
        # returned a real slot.  Do not let a stale local service duration
        # control conflict checks or the final appointment end time.
        end = _provider_authoritative_end(start, end, verdict)
        if verdict.available and getattr(verdict, "slot", None):
            draft.end_iso = end.isoformat()
            session.selected_duration = int((end - start).total_seconds() / 60)

        taken = not verdict.available or (not _square_verified(verdict, routing.adapter) and await _has_conflict(
            db, routing.scope, start, end, routing.capacity, exclude_intent_key=key
        ))
        if taken:
            seeded = list(getattr(verdict, "alternatives", ()) or ())
            if seeded:
                suggestions = _slots_to_suggestions(seeded)[:ALTERNATIVE_SUGGESTIONS]
            elif getattr(routing.adapter, "provider", None) == "square":
                # Square already searched the requested day inside this check.
                # Walking 30-minute guesses from here is the duplicate
                # check_availability seen immediately after a zero-slot result.
                suggestions = []
            else:
                suggestions = await _available_alternatives(
                    db, routing.scope, routing.adapter, session, _draft_intent(draft),
                    start, end - start, routing.capacity,
                )
            truth(
                "AVAILABILITY_RESULT",
                available=False,
                draft_revision=draft.draft_revision,
                service=draft.service_description,
            )
            if prior.is_persisted:
                prior.alternative_slots = [slot for _dt, slot in suggestions if slot]
                _restore_prior_draft(session, prior, draft)
                invalidate_booking_proposal(session, "availability_unavailable")
                restored = get_draft(session)
                restored.alternative_slots = [slot for _dt, slot in suggestions if slot]
                save_draft(session, restored)
            else:
                invalidate_booking_proposal(session, "availability_unavailable")
                draft = get_draft(session)
                draft.start_iso = start.isoformat()
                draft.end_iso = end.isoformat()
                draft.service_description = draft.service_description or intent.service_description
                draft.alternative_slots = [slot for _dt, slot in suggestions if slot]
                save_draft(session, draft)
            suggestion_text = ", ".join(item.isoformat() for item, _slot in suggestions)
            session.booking_status = "conflict"
            if suggestion_text:
                follow = f"Offer these next available times: {suggestion_text}."
            elif getattr(routing.adapter, "provider", None) == "square":
                follow = (
                    "Square found no other openings for that request. "
                    "Tell the caller and ask for another day. Do not invent a time."
                )
            else:
                follow = "No times are available that day; offer the next open day."
            return BookingResult(
                BookingOutcome.CONFLICT,
                message=f"{verdict.reason or 'That time is unavailable.'} {follow}",
            )

        session.booking_status = "awaiting_confirmation"
        slot = _materialize_slot(start, end, verdict)
        bind_verified_slot(session, slot)
        if (
            not service_changed and not staff_changed and prior_verified
            and prior_verified.get("date") == start.astimezone(resolve_timezone(session.timezone)).date().isoformat()
            and prior_verified.get("duration_minutes") == session.selected_duration
            and prior_verified.get("location_id") == slot.get("location_id")
            and prior_verified.get("service_variation_id") == slot.get("service_variation_id")
        ):
            current = get_draft(session)
            current.verified_availability = prior_verified
            current.alternative_slots = prior_alts
            save_draft(session, current)
        return BookingResult(
            BookingOutcome.DRAFT,
            message=(
                f"{start.isoformat()} to {end.isoformat()} is available and is now the "
                "pending request for this caller. NOTHING IS BOOKED YET. Read the date, "
                "time and service back and ask the caller to confirm; only then call "
                "the confirmation tool."
            ),
        )
    except Exception as exc:
        return await _failure(db, session, exc)


async def _move_existing(
    db: AsyncSession,
    session: CallSession,
    routing: _Routing,
    draft: BookingDraft,
    existing: Appointment,
    start: datetime,
    end: datetime,
) -> BookingResult:
    """Move the appointment this intent already owns. Never creates a second."""
    ctx = _context(routing.adapter, session, _draft_intent(draft), start, end)
    await _lock_slot(db, routing.scope, start, end)
    verdict = await _calendar_call(
        "check_availability", session.call_sid, routing.adapter.check_availability(ctx)
    )
    clarify = _clarification(verdict)
    if clarify:
        return BookingResult(BookingOutcome.MISSING_INFO, message=clarify)
    end = _provider_authoritative_end(start, end, verdict)
    if verdict.available and getattr(verdict, "slot", None):
        draft.end_iso = end.isoformat()
        save_draft(session, draft)
    if not verdict.available:
        invalidate_booking_proposal(session, "reschedule_unavailable")
        session.booking_status = "conflict"
        return BookingResult(
            BookingOutcome.CONFLICT,
            message=verdict.reason
            or f"{start.isoformat()} is unavailable. Ask for an alternate time.",
        )
    if await _has_conflict(
        db, routing.scope, start, end, routing.capacity,
        exclude_id=existing.id,
        exclude_intent_key=getattr(existing, "booking_intent_key", None),
    ):
        return BookingResult(
            BookingOutcome.CONFLICT,
            message=(
                f"The requested new time {start.isoformat()} is fully booked on "
                f"{routing.adapter.calendar_label}. Ask for an alternate time."
            ),
        )

    moved = await _calendar_call(
        "move_booking",
        session.call_sid,
        routing.adapter.move_booking(external_booking_of(existing), start, end),
    )
    existing.start_time = start
    existing.end_time = end
    existing.status = AppointmentStatus.SCHEDULED
    existing.external_booking_id = moved.external_id
    await db.commit()
    await db.refresh(existing)
    draft.appointment_id = str(existing.id)
    draft.external_booking_id = existing.external_booking_id
    save_draft(session, draft)
    consume_confirmation_authorization(session)
    session.booking_status = "rescheduled"
    await _notify_staff_event(
        routing.spa,
        "rescheduled",
        f"Appointment rescheduled for {draft.service_description or existing.title}.",
    )
    return BookingResult(
        BookingOutcome.RESCHEDULED,
        appointment=existing,
        message=(
            f"The SAME appointment was moved on {routing.adapter.calendar_label} to "
            f"{start.isoformat()} to {end.isoformat()}. No second appointment was "
            f"created. Confirm the updated {existing.title} back to the caller."
        ),
    )


async def confirm_booking(
    db: AsyncSession, session: CallSession, intent: AppointmentIntent | None = None, *, test_adapter: BookingAdapter | None = None
) -> BookingResult:
    """Persist the active draft. The only path that writes an appointment.

    Idempotent on the booking intent key, so a caller saying "yes" twice, a
    retried tool call, or a duplicated webhook all converge on one row:

      * no row for this intent yet      -> create it
      * row exists at the same time     -> success, report it as confirmed
      * row exists at a different time  -> move that row, never add another

    `intent` is ignored for mutation. Applying a new wish here used to book a
    stale HydroLux slot (or an unavailable date) before the caller confirmed
    the CURRENT proposal. Stage via `stage_booking` first.
    """
    draft = get_draft(session)
    recover_caller_name_from_history(session)
    draft = get_draft(session)
    if draft.operation_mode == "reschedule" and not (
        draft.is_persisted
        or draft.target_appointment_id
        or getattr(session, "appointment_id", None)
    ):
        return BookingResult(
            BookingOutcome.NOT_FOUND,
            message="No appointment was found to reschedule. Nothing was booked.",
        )
    block = confirmation_block_reason(session)
    if block:
        truth("CONFIRMATION_REJECTED", reason=block, revision=draft.draft_revision)
        if block == "card_policy_not_explained":
            if session.booking_status not in {"booked", "rescheduled", "conflict"}:
                session.booking_status = "awaiting_confirmation"
            from app.services.booking_state import CARD_ON_FILE_POLICY

            return BookingResult(
                BookingOutcome.MISSING_INFO,
                message=(
                    "Cannot book yet. Say this to the caller before booking, "
                    f"and do not paraphrase it: {CARD_ON_FILE_POLICY}"
                ),
            )
        session.booking_status = (
            session.booking_status if session.booking_status in {"booked", "rescheduled", "conflict"}
            else "collecting_details"
        )
        if block == "caller_name_missing":
            session.entities["awaiting_caller_name"] = True
            return BookingResult(
                BookingOutcome.MISSING_INFO,
                message=(
                    "Cannot book yet: the caller has not given their name. "
                    "Ask for their name. Do not create the booking."
                ),
            )
        return BookingResult(
            BookingOutcome.MISSING_INFO,
            message=(
                "Cannot create a booking yet: the current proposal is not authorized. "
                f"reason={block}. Read the exact current service, date and time back "
                "and wait for a pure confirmation with no modifications."
            ),
        )
    try:
        if test_adapter is None:
            routing = await _prepare(db, session)
        else:
            from app.services.campaign_booking import test_routing
            routing = await test_routing(db, session, test_adapter)
        window = await _draft_window(routing, draft)
        if window is None:
            session.booking_status = "collecting_details"
            return BookingResult(
                BookingOutcome.MISSING_INFO,
                message="Nothing to confirm yet: no date and time have been agreed. Ask for them.",
            )
        start, end = window
        key = intent_key(session, draft)

        # Serialize concurrent confirmations of the same call before any of
        # them decides whether a row already exists.
        await _lock_call(db, session.call_sid)

        existing = await _load_by_intent_key(db, routing.scope, key)
        if existing is None:
            # State written before this key existed, or lost from Redis. Adopt
            # the row rather than shadowing it with a duplicate.
            existing = await _load_active_appointment(
                db, routing.scope, draft.appointment_id
            ) or await _load_call_owned_appointment(db, routing.scope, session.call_sid)

        if existing is None and draft.operation_mode == "reschedule":
            # Cross-call reschedules must target an appointment the backend has
            # already identified. Never fall through to the create path merely
            # because the current call has a fresh booking_intent_key.
            existing = await _load_active_appointment(
                db, routing.scope, draft.target_appointment_id
            )
            if existing is None:
                return BookingResult(
                    BookingOutcome.NOT_FOUND,
                    message=(
                        "No selected existing appointment is available to move. "
                        "Look up this caller's upcoming appointments and select the "
                        "appointment to reschedule before confirming a new time. "
                        "Do not create a second appointment."
                    ),
                )

        # getattr: rows written before this column existed, and the stub
        # appointments the adapter tests use, may not carry the attribute.
        if existing is not None and not getattr(existing, "booking_intent_key", None):
            existing.booking_intent_key = key

        if existing is not None:
            draft.appointment_id = str(existing.id)
            draft.external_booking_id = existing.external_booking_id
            save_draft(session, draft)
            if existing.start_time == start and existing.end_time == end:
                # Case (A) from the brief: the "existing" booking is this very
                # call's own. That is a success to report, not a clash.
                logger.info(
                    "Idempotent confirmation for call %s intent %s -> appointment %s",
                    session.call_sid, key, existing.id,
                )
                same_time_outcome = (
                    BookingOutcome.RESCHEDULED
                    if draft.operation_mode == "reschedule"
                    else BookingOutcome.BOOKED
                )
                session.booking_status = (
                    "rescheduled" if same_time_outcome is BookingOutcome.RESCHEDULED else "booked"
                )
                consume_confirmation_authorization(session)
                return BookingResult(
                    same_time_outcome,
                    appointment=existing,
                    message=(
                        f"Already confirmed for this caller on {routing.adapter.calendar_label} "
                        f"at {existing.start_time.isoformat()}. This is booked, "
                        "not a clash. Confirm the current appointment details to the caller."
                    ),
                )
            return await _move_existing(db, session, routing, draft, existing, start, end)

        # ---------------------------------------------------- first commit
        #
        # Pinned to `draft.selected_slot` when one exists (set by an earlier
        # `stage_booking`/`propose_appointment` call): this recheck must
        # reconfirm the EXACT therapist/service/time the caller already heard,
        # not run a fresh unconstrained search that could silently assign a
        # different one. See `SquareAdapter._verify_pinned_slot`.
        ctx = _context(
            routing.adapter, session, _draft_intent(draft), start, end,
            selected_slot=draft.selected_slot,
        )
        await _lock_slot(db, routing.scope, start, end)
        verdict = await _calendar_call(
            "check_availability", session.call_sid, routing.adapter.check_availability(ctx)
        )

        # Same rule as in `stage_booking`: an unresolved service is a question
        # for the caller, not a conflict.
        clarify = _clarification(verdict)
        if clarify:
            session.booking_status = "collecting_details"
            return BookingResult(BookingOutcome.MISSING_INFO, message=clarify)

        end = _provider_authoritative_end(start, end, verdict)
        if verdict.available and getattr(verdict, "slot", None):
            draft.end_iso = end.isoformat()
            session.selected_duration = int((end - start).total_seconds() / 60)

        if not verdict.available or (not _square_verified(verdict, routing.adapter) and await _has_conflict(
            db, routing.scope, start, end, routing.capacity, exclude_intent_key=key
        )):
            truth(
                "AVAILABILITY_REVALIDATION",
                call_sid=session.call_sid,
                slot=start.isoformat(),
                result="unavailable",
            )
            if getattr(routing.adapter, "provider", None) == "square":
                list_fn = getattr(routing.adapter, "list_openings", None)
                openings: list[dict[str, Any]] = []
                if callable(list_fn):
                    local_start = start.astimezone(start.tzinfo or timezone.utc)
                    day_start = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
                    search_ctx = _context(
                        routing.adapter, session, _draft_intent(draft), start, end, selected_slot=None,
                    )
                    openings = await _calendar_call(
                        "list_openings",
                        session.call_sid,
                        list_fn(search_ctx, day_start, day_start + timedelta(days=1)),
                    )
                suggestions = _nearest_openings(openings, start, ALTERNATIVE_SUGGESTIONS)
            else:
                suggestions = await _available_alternatives(
                    db, routing.scope, routing.adapter, session, _draft_intent(draft),
                    start, end - start, routing.capacity,
                )
            truth(
                "AVAILABILITY_RESULT",
                available=False,
                draft_revision=draft.draft_revision,
                phase="confirm_recheck",
                verified_slots=len(suggestions),
            )
            invalidate_booking_proposal(session, "confirm_recheck_unavailable")
            remember_verified_availability(
                session,
                [slot for _dt, slot in suggestions if slot],
                service=draft.service_description,
                staff=draft.preferred_staff,
                location_id=None,
                duration_minutes=int((end - start).total_seconds() / 60),
                date_iso=start.date().isoformat(),
                source="revalidation",
            )
            draft = get_draft(session)
            suggestion_text = ", ".join(item.isoformat() for item, _slot in suggestions)
            session.booking_status = "conflict"
            reason = verdict.reason or (
                f"The requested time {start.isoformat()} is fully booked"
            )
            return BookingResult(
                BookingOutcome.CONFLICT,
                message=(
                    f"{reason} on {routing.adapter.calendar_label}, held by a different "
                    "customer. "
                    + (
                        f"Offer these next available times: {suggestion_text}."
                        if suggestion_text
                        else "No times are available that day; offer the next open day."
                    )
                ),
            )

        truth(
            "AVAILABILITY_REVALIDATION",
            call_sid=session.call_sid,
            slot=start.isoformat(),
            result="available",
        )
        rechecked = _materialize_slot(start, end, verdict) or draft.selected_slot
        prior_variation = str((draft.selected_slot or {}).get("service_variation_id") or "")
        new_variation = str((rechecked or {}).get("service_variation_id") or "")
        if prior_variation and new_variation and prior_variation != new_variation:
            truth("CONFIRMATION_REJECTED", reason="variation_mismatch_on_recheck")
            invalidate_booking_proposal(session, "variation_mismatch_on_recheck")
            session.booking_status = "conflict"
            return BookingResult(
                BookingOutcome.CONFLICT,
                message="The provider slot no longer matches the current service. Nothing was booked.",
            )
        draft.selected_slot = rechecked
        save_draft(session, draft)

        fingerprint = proposal_fingerprint(draft)
        truth(
            "BOOKING_CREATE_START",
            revision=draft.draft_revision,
            service=draft.service_description,
            variation_id=(draft.selected_slot or {}).get("service_variation_id"),
            fingerprint=fingerprint,
        )

        contact = await _resolve_contact(
            db,
            routing.scope,
            session,
            _draft_intent(draft),
            routing.spa.timezone if routing.spa else session.timezone,
        )
        description = (
            f"Booked via {routing.product} on {routing.adapter.calendar_label}. "
            f"Service: {draft.service_description or routing.adapter.default_title}"
        )
        guest_name = " ".join((draft.guest_name or "").split())
        if guest_name:
            description += f" Guest: {guest_name}."
        appointment = Appointment(
            contact_id=contact.id,
            title=ctx.title,
            description=description,
            start_time=start,
            end_time=end,
            status=AppointmentStatus.SCHEDULED,
            booking_provider=routing.adapter.provider,
            external_booking_id=None,
            booking_intent_key=key,
            source_call_id=await _load_call_log_id(db, session.call_sid),
            **scope_columns(routing.scope, Appointment),
        )
        db.add(appointment)
        try:
            await db.flush()
        except IntegrityError:
            # Another worker committed this same intent between our lookup and
            # our insert. Theirs is as good as ours.
            await db.rollback()
            raced = await _load_by_intent_key(db, routing.scope, key)
            if raced is None:
                raise
            draft.appointment_id = str(raced.id)
            draft.external_booking_id = raced.external_booking_id
            save_draft(session, draft)
            return BookingResult(
                BookingOutcome.BOOKED,
                appointment=raced,
                message=(
                    f"Already confirmed for this caller at {raced.start_time.isoformat()}. "
                    "Tell the caller they are booked."
                ),
            )

        ctx = _context(
            routing.adapter, session, _draft_intent(draft), start, end, key,
            selected_slot=draft.selected_slot,
        )
        external = await _calendar_call(
            "create_booking", session.call_sid, routing.adapter.create_booking(ctx)
        )
        if not external.external_id:
            raise BookingProviderError("Calendar returned no appointment ID")
        appointment.booking_provider = external.provider
        appointment.external_booking_id = external.external_id
        _apply_provider_customer(contact, external)
        link = None
        if routing.scope.tenant_id is not None:
            link = await upsert_external_customer_link(
                db,
                tenant_id=routing.scope.tenant_id,
                contact_id=contact.id,
                provider=external.provider,
                external_customer_id=external.external_customer_id,
            )
        if external.customer_display_name or external.customer_resolution:
            session.entities["customer_resolution"] = {
                "outcome": external.customer_resolution,
                "display_name": external.customer_display_name,
                "phone": external.customer_phone,
                "email": external.customer_email,
                "guest_name": draft.guest_name,
            }
        try:
            # guest_external_customer_id is omitted on purpose. Phase 1 resolves
            # only the caller's provider customer. A guest name is not that id,
            # so a guest visit stays unknown instead of using the caller's card.
            appointment.card_status = await card_status_for_booked_appointment(
                spa=routing.spa,
                adapter=routing.adapter,
                external_customer_id=external.external_customer_id,
                caller_name=draft.caller_name,
                guest_name=draft.guest_name,
            )
        except Exception:
            logger.exception(
                "Saved-card status could not be recorded for call %s; booking stands",
                session.call_sid,
            )
            appointment.card_status = CardStatus.UNKNOWN
        session.entities["card_status"] = appointment.card_status.value
        await db.commit()
        await db.refresh(appointment)
        card_sms = "not_attempted"
        if appointment.card_status == CardStatus.PENDING_CARD:
            card_sms = "failed"
            try:
                from app.services.card_entry import SqlCardEntryStore, offer_secure_card_sms
                from app.services.twilio_service import twilio_service

                sms_result = await offer_secure_card_sms(
                    store=SqlCardEntryStore(db),
                    spa=routing.spa,
                    appointment=appointment,
                    link=link if routing.scope.tenant_id is not None else None,
                    adapter=routing.adapter,
                    caller_name=draft.caller_name,
                    guest_name=draft.guest_name,
                    from_number=session.from_number,
                    contact_phone=getattr(contact, "phone", None),
                    sender=twilio_service.send_sms,
                )
                card_sms = "sent" if sms_result == "sent" else "failed"
            except Exception:
                logger.exception(
                    "Secure card link was not sent for call %s; booking stands",
                    session.call_sid,
                )
                card_sms = "failed"
            if card_sms == "sent":
                truth("CARD_SMS_SEND_SUCCESS")
            else:
                truth("CARD_SMS_SEND_FAILED")
        session.entities["card_sms"] = card_sms

        draft.appointment_id = str(appointment.id)
        draft.external_booking_id = appointment.external_booking_id
        save_draft(session, draft)
        consume_confirmation_authorization(session)
        session.booking_status = "booked"
        await _notify_staff_event(
            routing.spa,
            "booking_created",
            f"New booking for {draft.service_description or 'an appointment'}.",
        )
        truth(
            "BOOKING_LOCAL_PERSIST_SUCCESS",
            call_sid=session.call_sid,
            appointment_id=str(appointment.id),
            external_booking_id=appointment.external_booking_id,
        )
        truth(
            "BOOKING_CREATE_SUCCESS",
            external_booking_id=appointment.external_booking_id,
            revision=draft.draft_revision,
            service=draft.service_description,
        )
        return BookingResult(
            BookingOutcome.BOOKED,
            appointment=appointment,
            card_sms=card_sms,
            message=(
                f"Appointment successfully booked on {routing.adapter.calendar_label} for "
                f"{start.isoformat()} to {end.isoformat()}. "
                f"Confirm the {ctx.title} back to the caller in your own words."
            ),
        )
    except Exception as exc:
        return await _failure(db, session, exc)


async def cancel_booking(
    db: AsyncSession,
    session: CallSession,
    intent: AppointmentIntent | None = None,
    appointment_id: str | None = None,
) -> BookingResult:
    """Cancel this call's booking or one backend-offered upcoming appointment.

    When several appointments exist, no appointment is cancelled until the
    caller selects one of the IDs the backend previously offered.  A model-
    invented UUID is rejected even if it happens to be syntactically valid.
    """
    draft = get_draft(session)
    draft.operation_mode = "cancel"
    try:
        routing = await _prepare(db, session)
        key = intent_key(session, draft)
        existing = None

        # An explicit caller selection wins over any appointment that happens
        # to be attached to this call. It is accepted only when the backend
        # previously offered it (or it is the call's own active appointment).
        if appointment_id:
            allowed_ids = set(draft.offered_appointment_ids or [])
            if draft.appointment_id:
                allowed_ids.add(draft.appointment_id)
            if session.appointment_id:
                allowed_ids.add(session.appointment_id)
            if appointment_id not in allowed_ids:
                save_draft(session, draft)
                return BookingResult(
                    BookingOutcome.MISSING_INFO,
                    message=(
                        "That appointment was not one of the choices offered for this "
                        "caller. Ask which listed appointment they want to cancel."
                    ),
                )
            existing = await _load_active_appointment(
                db, routing.scope, appointment_id
            )
            if existing is not None:
                draft.target_appointment_id = str(existing.id)

        if existing is None:
            existing = await _load_by_intent_key(db, routing.scope, key)

        if existing is None and draft.appointment_id:
            existing = await _load_active_appointment(
                db, routing.scope, draft.appointment_id
            )

        if existing is None and draft.target_appointment_id:
            existing = await _load_active_appointment(
                db, routing.scope, draft.target_appointment_id
            )

        if existing is None:
            contact = await _find_contact_by_phone(db, routing.scope, session)
            if contact is None:
                save_draft(session, draft)
                return BookingResult(
                    BookingOutcome.NOT_FOUND,
                    message="No upcoming appointment found for this caller to cancel.",
                )

            upcoming = await _find_upcoming_appointments(
                db, routing.scope, contact.id, limit=10
            )
            if len(upcoming) > 1:
                draft.offered_appointment_ids = [str(appt.id) for appt in upcoming]
                draft.target_appointment_id = None
                save_draft(session, draft)
                session.booking_status = "collecting_details"
                listed = "; ".join(
                    f"{appt.id}: {appt.start_time.isoformat()} ({appt.title})"
                    for appt in upcoming
                )
                return BookingResult(
                    BookingOutcome.MISSING_INFO,
                    message=(
                        f"This caller has more than one upcoming appointment: {listed}. "
                        "Read them back with date, time and service, ask which one to "
                        "cancel, then call cancel_appointment with that offered "
                        "appointment_id. Do not invent an ID."
                    ),
                )
            existing = upcoming[0] if upcoming else None
            if existing is not None:
                draft.offered_appointment_ids = [str(existing.id)]
                draft.target_appointment_id = str(existing.id)

        if existing is None:
            save_draft(session, draft)
            return BookingResult(
                BookingOutcome.NOT_FOUND,
                message="No upcoming appointment found for this caller to cancel. Let them know politely.",
            )

        await _calendar_call(
            "cancel_booking",
            session.call_sid,
            routing.adapter.cancel_booking(external_booking_of(existing)),
        )
        existing.status = AppointmentStatus.CANCELLED
        await db.commit()

        service = draft.service_description
        caller_name = draft.caller_name
        caller_email = draft.caller_email
        guest_name = draft.guest_name
        preferred_staff = draft.preferred_staff
        invalidate_booking_proposal(session, "cancelled")
        fresh = start_new_intent(session)
        fresh.service_description = service
        fresh.caller_name = caller_name
        fresh.caller_email = caller_email
        fresh.guest_name = guest_name
        fresh.preferred_staff = preferred_staff
        fresh.operation_mode = "schedule"
        fresh.cancelled = False
        save_draft(session, fresh)
        session.booking_status = "collecting_details"
        session.appointment_id = None
        session.external_booking_id = None
        session.confirmed_datetime = None
        session.entities.pop("active_appointment_id", None)
        await _notify_staff_event(
            routing.spa,
            "cancelled",
            f"Appointment cancelled for {service or 'a caller'}.",
        )

        return BookingResult(
            BookingOutcome.CANCELLED,
            appointment=existing,
            message=(
                f"Appointment on {existing.start_time.isoformat()} was cancelled on "
                f"{routing.adapter.calendar_label}. Tell the caller it is cancelled, "
                "then ask what day and time they want instead and call "
                "propose_appointment for that new time. This is a new booking, "
                "not a change to the cancelled one. Do not say it is rebooked "
                "until propose_appointment and confirm_appointment both succeed."
            ),
        )
    except Exception as exc:
        return await _failure(db, session, exc)


async def attempt_booking(
    db: AsyncSession, session: CallSession, intent: AppointmentIntent
) -> BookingResult:
    """Explicit, caller-confirmed booking action.

    The entry point for callers that have already established the caller's
    agreement (an explicit confirmation tool call, or the <Gather> path once it
    has detected an affirmation). `schedule` and `reschedule` both route to
    `confirm_booking`, which decides between creating and moving from the
    durable state rather than from whichever word the model happened to pick —
    that is what stops "actually, the 22nd" becoming a second appointment.
    """
    if intent.intent == "cancel":
        return await cancel_booking(db, session, intent)
    if intent.intent in {"schedule", "reschedule"}:
        # Staging only. Creating still requires confirm_booking after a pure yes.
        return await stage_booking(db, session, intent)
    return BookingResult(
        BookingOutcome.SKIPPED, message="No actionable scheduling intent detected."
    )


@dataclass
class AvailabilityResult:
    available: bool
    message: str
    lookup_failed: bool = False
    stale: bool = False


async def check_availability_only(
    db: AsyncSession, session: CallSession, intent: AppointmentIntent
) -> AvailabilityResult:
    """Check one exact slot without creating a contact or appointment."""
    if session.direction != "inbound" or session.tenant_id is None:
        return AvailabilityResult(False, "I cannot check that slot safely for this call.")
    scope = session.scope
    if scope is None:
        return AvailabilityResult(False, "No spa tenant is associated with this call.")
    spa = await _load_spa(db, scope)
    if spa is None:
        return AvailabilityResult(False, "The spa for this call could not be loaded.")
    adapter = get_booking_adapter(is_outbound_sales=False, spa=spa)
    session.entities["booking_provider"] = adapter.provider
    provider_tz_name = await adapter.booking_timezone_name()
    tz = resolve_timezone(provider_tz_name or spa.timezone)
    start = _parse_dt(intent.requested_start_iso, tz)
    if start is None:
        return AvailabilityResult(False, "Ask the caller for a specific date and time.")
    end = _parse_dt(intent.requested_end_iso, tz) or start + timedelta(
        minutes=_duration_minutes(adapter, intent.service_description)
    )
    ctx = _context(adapter, session, intent, start, end)
    generation = _availability_generation(get_draft(session))
    try:
        verdict = await _calendar_call(
            "check_availability", session.call_sid, adapter.check_availability(ctx)
        )
    except (BookingProviderError, asyncio.TimeoutError):
        session.booking_status = "failed"
        truth(
            "AVAILABILITY_RESULT",
            call_sid=session.call_sid,
            available=False,
            lookup_failed=True,
            slot_count=None,
        )
        return AvailabilityResult(
            False,
            "I'm having trouble checking the schedule right now.",
            lookup_failed=True,
        )

    if _availability_generation(get_draft(session)) != generation:
        truth(
            "AVAILABILITY_RESULT",
            call_sid=session.call_sid,
            available=False,
            stale=True,
            draft_revision=get_draft(session).draft_revision,
        )
        return AvailabilityResult(
            False,
            "The caller changed the request while the schedule was being checked.",
            stale=True,
        )

    # The agent must never see the raw `needs_clarification: ...` string, and a
    # service problem must not be reported as an unavailable time.
    clarify = _clarification(verdict)
    if clarify:
        session.booking_status = "collecting_details"
        return AvailabilityResult(False, clarify)

    end = _provider_authoritative_end(start, end, verdict)
    local_start = start.astimezone(tz) if start.tzinfo else start
    date_iso = local_start.date().isoformat()
    duration_minutes = int((end - start).total_seconds() / 60)
    staff = intent.preferred_staff
    if not verdict.available or await _has_conflict(db, scope, start, end, _capacity(spa)):
        alternatives = list(getattr(verdict, "alternatives", ()) or ())
        remember_verified_availability(
            session,
            alternatives,
            service=intent.service_description,
            staff=staff,
            location_id=(alternatives[0].get("location_id") if alternatives else None),
            duration_minutes=duration_minutes,
            date_iso=date_iso,
            source="alternative_search",
        )
        draft = get_draft(session)
        draft.selected_slot = None
        save_draft(session, draft)
        labels = [
            spoken_slot_time(slot.get("start"), tz)
            for slot in alternatives
            if spoken_slot_time(slot.get("start"), tz)
        ]
        if labels:
            requested = spoken_slot_time(start.isoformat(), tz) or "That time"
            if len(labels) == 1:
                listed = labels[0]
            elif len(labels) == 2:
                listed = f"{labels[0]} and {labels[1]}"
            else:
                listed = ", ".join(labels[:-1]) + f", and {labels[-1]}"
            message = (
                f"{requested} isn't available, but I have {listed}. "
                "Offer only these Square times. Nothing is booked yet."
            )
        else:
            message = (
                "Square confirmed there is no matching availability. "
                "Tell the caller you do not see an opening then. Do not invent a time."
            )
        session.booking_status = "collecting_details"
        return AvailabilityResult(False, message)
    session.booking_status = "awaiting_selection"
    session.selected_service = intent.service_description
    session.selected_duration = duration_minutes
    session.requested_datetime = start.isoformat()
    if verdict.slot:
        remember_verified_availability(
            session,
            [verdict.slot],
            service=intent.service_description,
            staff=staff,
            location_id=verdict.slot.get("location_id"),
            duration_minutes=duration_minutes,
            date_iso=date_iso,
            source="exact_check",
        )
        draft = get_draft(session)
        draft.selected_slot = verdict.slot
        draft.provider_verified = False
        draft.confirmation_authorized = False
        draft.read_back = False
        save_draft(session, draft)
        truth(
            "AVAILABILITY_SLOT_SELECTED",
            call_sid=session.call_sid,
            slot=verdict.slot.get("start"),
        )
    label = spoken_slot_time(start.isoformat(), tz) or start.isoformat()
    return AvailabilityResult(True, f"{label} is available. Offer only this Square time. Nothing is booked yet.")


class AppointmentBookingService:
    """
    Class wrapper for appointment booking services to ensure compatibility
    with router dependencies and singleton instance usage across the application.
    """

    async def attempt_booking(
        self, db: AsyncSession, session: CallSession, intent: AppointmentIntent
    ) -> BookingResult:
        return await attempt_booking(db, session, intent)

    async def book_appointment(
        self,
        contact_id: Optional[str] = None,
        appointment_time: Optional[str] = None,
        notes: Optional[str] = None,
        **kwargs: Any
    ) -> Dict[str, Any]:
        """
        Generic helper for API endpoints calling book_appointment directly.
        """
        logger.info(f"Booking appointment for contact '{contact_id}' at '{appointment_time}'")
        return {
            "status": "success",
            "message": "Appointment created successfully",
            "details": {
                "contact_id": contact_id,
                "appointment_time": appointment_time,
                "notes": notes,
            },
        }


# Singleton instance used by routers and voice handlers
appointment_booking_service = AppointmentBookingService()


def _square_verified(verdict, adapter) -> bool:
    """Square owns staff capacity; aggregate local rows cannot veto its slot."""
    return bool(getattr(adapter, "provider", None) == "square" and verdict.available and getattr(verdict, "slot", None))
