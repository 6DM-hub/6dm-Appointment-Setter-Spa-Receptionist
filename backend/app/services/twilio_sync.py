"""
Backfill call history from Twilio into `call_logs`.

The dashboard reads `call_logs`, so a call only appears on the website if this
application handled it. Calls arriving over the Elastic SIP trunk never do —
the trunk overrides the number's voice_url, so no webhook fires and no row is
ever written (see app/api/v1/xai_voice.py for the long-term fix). Until inbound
calls come back through the app, this reconciles the Twilio account's own call
records into the same table so the history is at least complete and correctly
scoped.

Metadata-only for rows the live flow already finalized (`ended_at` set) — Twilio
knows who called, when, for how long and with what outcome, but the
conversation happened inside xAI's voice agent or the <Gather> pipeline, so a
sync must never erase a transcript the app already captured.

For a row Twilio reports as terminal but the app never finalized — the caller
hangs up mid-<Gather> with no further webhook firing at all — this is also the
safety net that pulls whatever transcript is still sitting in Redis onto the
row before it expires. See `app.services.call_finalization` for why this can't
be the *only* path (it runs on a multi-minute interval) but is needed as a
backstop for both the request-driven finalizers.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import redis_manager
from app.models import CallDirection, CallLog, CallStatus, Contact, SpaAccount, User
from app.services.call_finalization import finalize_gather_call
from app.services.call_state import CallStateStore
from app.services.phone_numbers import normalize_phone_target

logger = logging.getLogger(__name__)

# Twilio's `direction` values. `trunking-originating` is a call arriving from the
# PSTN and leaving over our SIP trunk — inbound from the business's point of
# view, even though Twilio names it from the trunk's.
_INBOUND_DIRECTIONS = {"inbound", "trunking-originating"}
_OUTBOUND_DIRECTIONS = {"outbound-api", "outbound-dial", "trunking-terminating"}

_STATUS_MAP = {
    "queued": CallStatus.QUEUED,
    "initiated": CallStatus.QUEUED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.IN_PROGRESS,
    "answered": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.BUSY,
    "failed": CallStatus.FAILED,
    "no-answer": CallStatus.NO_ANSWER,
    "canceled": CallStatus.CANCELLED,
}

_TERMINAL = {
    CallStatus.COMPLETED,
    CallStatus.BUSY,
    CallStatus.FAILED,
    CallStatus.NO_ANSWER,
    CallStatus.CANCELLED,
}


@dataclass
class SyncReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    unattributed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "unattributed": self.unattributed,
        }


@dataclass
class _CallRecord:
    """The subset of a Twilio call we persist, normalised."""

    sid: str
    direction: CallDirection
    status: CallStatus
    from_number: str
    to_number: str
    dialed_number: str | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _caller_number(call: Any) -> str | None:
    """The calling party on a Twilio call resource.

    The JSON field is `from`, which the SDK cannot expose under that name. On a
    call *instance* it lands on `_from`; `from_` is only the keyword argument to
    `calls.create`. Reading the wrong one yields None, which silently turned
    every synced caller ID into "unknown" and left outbound calls
    unattributable — so both spellings are tried.
    """
    for attr in ("_from", "from_"):
        value = normalize_phone_target(getattr(call, attr, None))
        if value:
            return value
    return None


def _normalise(call: Any) -> _CallRecord | None:
    """Map one Twilio call resource onto a `_CallRecord`.

    Returns None for directions we do not model, rather than guessing.
    """
    raw_direction = (getattr(call, "direction", "") or "").lower()
    if raw_direction in _INBOUND_DIRECTIONS:
        direction = CallDirection.INBOUND
    elif raw_direction in _OUTBOUND_DIRECTIONS:
        direction = CallDirection.OUTBOUND
    else:
        logger.info("Skipping call %s with unmodelled direction %r", call.sid, raw_direction)
        return None

    from_number = _caller_number(call) or "unknown"
    to_number = normalize_phone_target(getattr(call, "to", None)) or "unknown"

    # Which of our numbers this call belongs to. On a trunked inbound call `to`
    # is a SIP URI and `forwarded_from` carries the number that was dialled.
    if direction is CallDirection.INBOUND:
        dialed = normalize_phone_target(getattr(call, "forwarded_from", None)) or to_number
    else:
        dialed = from_number

    return _CallRecord(
        sid=call.sid,
        direction=direction,
        status=_STATUS_MAP.get((getattr(call, "status", "") or "").lower(), CallStatus.QUEUED),
        from_number=from_number,
        to_number=to_number,
        dialed_number=dialed if dialed != "unknown" else None,
        started_at=_as_utc(getattr(call, "start_time", None)),
        ended_at=_as_utc(getattr(call, "end_time", None)),
        duration_seconds=_as_int(getattr(call, "duration", None)),
    )


async def _attribute(
    db: AsyncSession, record: _CallRecord
) -> tuple[SpaAccount | None, User | None]:
    """Decide which spa or workspace owns this call.

    Scoping is what makes a row visible: `call_logs` rows with both `tenant_id`
    and `user_id` NULL match no `scope_filter` and are therefore invisible to
    every user, so an unattributable call is reported rather than written blind.
    """
    if not record.dialed_number:
        return None, None

    spa = (
        await db.execute(
            select(SpaAccount).where(SpaAccount.twilio_phone_number == record.dialed_number)
        )
    ).scalar_one_or_none()
    if spa:
        return spa, None

    owner = (
        await db.execute(
            select(User).where(User.twilio_phone_number == record.dialed_number)
        )
    ).scalar_one_or_none()
    if owner:
        return None, owner

    # Fall back to the sole super admin, so a call to a number nobody has
    # claimed still lands somewhere a human can see it.
    admins = (
        await db.execute(select(User).where(User.role == "super_admin").limit(2))
    ).scalars().all()
    return None, (admins[0] if len(admins) == 1 else None)


async def _find_contact(
    db: AsyncSession, record: _CallRecord, spa: SpaAccount | None, owner: User | None
) -> Contact | None:
    customer = record.from_number if record.direction is CallDirection.INBOUND else record.to_number
    if customer == "unknown":
        return None
    query = select(Contact).where(Contact.phone_number == customer)
    if spa:
        query = query.where(Contact.tenant_id == spa.id)
    elif owner:
        query = query.where(Contact.owner_id == owner.id)
    else:
        return None
    return (await db.execute(query)).scalars().first()


async def sync_calls(db: AsyncSession, *, limit: int = 50) -> SyncReport:
    """Reconcile the most recent `limit` Twilio calls into `call_logs`."""
    from app.services.twilio_service import twilio_service

    loop = asyncio.get_running_loop()
    calls = await loop.run_in_executor(
        None, partial(twilio_service.list_recent_calls, limit=limit)
    )

    report = SyncReport()
    existing_rows = {
        row.twilio_call_sid: row
        for row in (
            await db.execute(
                select(CallLog).where(
                    CallLog.twilio_call_sid.in_([c.sid for c in calls] or ["-"])
                )
            )
        ).scalars()
    }

    for call in calls:
        record = _normalise(call)
        if record is None:
            continue

        row = existing_rows.get(record.sid)
        if row is not None:
            # Only ever fill in progress; never rewrite a transcript or downgrade
            # a call the live flow already finished.
            changed = False
            if record.status in _TERMINAL and row.transcript is None:
                # Twilio says this call is over but no transcript ever landed
                # on this row — either nothing in the app finalized it (the
                # caller hung up mid-<Gather> before the app's own hangup path
                # ran, and no status callback fired either), or this row's
                # `ended_at` was set by this same metadata-only merge on an
                # earlier tick, which never touches transcript. Checking
                # `transcript` rather than `ended_at` is what keeps retrying
                # instead of leaving such a row silently stuck NULL forever.
                state = CallStateStore(redis_manager.client)
                changed = await finalize_gather_call(
                    db,
                    state,
                    record.sid,
                    status=record.status,
                    duration_seconds=record.duration_seconds,
                )
            else:
                if row.status not in _TERMINAL and row.status != record.status:
                    row.status = record.status
                    changed = True
                if row.ended_at is None and record.ended_at is not None:
                    row.ended_at = record.ended_at
                    changed = True
                if row.duration_seconds is None and record.duration_seconds is not None:
                    row.duration_seconds = record.duration_seconds
                    changed = True
            if row.started_at is None and record.started_at is not None:
                row.started_at = record.started_at
                changed = True
            # Repair a caller ID an earlier sync could not read. Only ever
            # replaces the "unknown" placeholder, never a real number.
            if row.from_number == "unknown" and record.from_number != "unknown":
                row.from_number = record.from_number
                changed = True
            if row.to_number == "unknown" and record.to_number != "unknown":
                row.to_number = record.to_number
                changed = True
            report.updated += 1 if changed else 0
            report.unchanged += 0 if changed else 1
            continue

        spa, owner = await _attribute(db, record)
        if spa is None and owner is None:
            # Writing this would create a row no user can query.
            report.unattributed.append(record.sid)
            logger.warning(
                "Cannot attribute Twilio call %s (dialed %s); skipping",
                record.sid,
                record.dialed_number,
            )
            continue

        contact = await _find_contact(db, record, spa, owner)
        db.add(
            CallLog(
                twilio_call_sid=record.sid,
                direction=record.direction,
                status=record.status,
                from_number=record.from_number,
                to_number=record.to_number,
                contact_id=contact.id if contact else None,
                user_id=owner.id if owner else None,
                tenant_id=spa.id if spa else None,
                started_at=record.started_at,
                ended_at=record.ended_at,
                duration_seconds=record.duration_seconds,
            )
        )
        report.created += 1

    await db.commit()
    logger.info("Twilio sync: %s", report.as_dict())
    return report


async def run_periodic_sync() -> None:
    """Reconcile call history on a loop for the life of the process.

    Started from the app lifespan. Failures are logged and retried on the next
    tick rather than killing the loop — a Twilio outage or a bad credential must
    not permanently stop call history from updating.
    """
    from app.core.database import AsyncSessionLocal

    interval = max(settings.TWILIO_SYNC_INTERVAL_SECONDS, 30)
    logger.info("Twilio call-history sync running every %ss", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            async with AsyncSessionLocal() as db:
                await sync_calls(db, limit=settings.TWILIO_SYNC_LIMIT)
        except asyncio.CancelledError:
            logger.info("Twilio call-history sync stopped")
            raise
        except Exception:
            logger.exception("Twilio call-history sync failed; retrying next tick")


async def repair_unscoped_call_logs(db: AsyncSession) -> dict[str, Any]:
    """Re-attribute rows that belong to nobody.

    `voice_inbound` writes `tenant_id=None, user_id=None` when the dialed number
    matches no spa and no workspace user. Such rows satisfy no `scope_filter`,
    so they are invisible on the website even though the call was logged and
    transcribed. This assigns them to whoever owns the dialed number now.
    """
    orphans = (
        await db.execute(
            select(CallLog).where(
                CallLog.tenant_id.is_(None), CallLog.user_id.is_(None)
            )
        )
    ).scalars().all()

    repaired, still_orphaned = 0, []
    for row in orphans:
        dialed = (
            row.to_number if row.direction is CallDirection.INBOUND else row.from_number
        )
        spa = (
            await db.execute(
                select(SpaAccount).where(SpaAccount.twilio_phone_number == dialed)
            )
        ).scalar_one_or_none()
        if spa is not None:
            row.tenant_id = spa.id
            repaired += 1
            continue

        owner = (
            await db.execute(select(User).where(User.twilio_phone_number == dialed))
        ).scalar_one_or_none()
        if owner is None:
            admins = (
                await db.execute(select(User).where(User.role == "super_admin").limit(2))
            ).scalars().all()
            owner = admins[0] if len(admins) == 1 else None
        if owner is not None:
            row.user_id = owner.id
            repaired += 1
        else:
            still_orphaned.append(row.twilio_call_sid)

    await db.commit()
    result = {
        "examined": len(orphans),
        "repaired": repaired,
        "still_unattributable": still_orphaned,
    }
    logger.info("Unscoped call log repair: %s", result)
    return result
