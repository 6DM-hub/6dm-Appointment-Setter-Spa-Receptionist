"""End-of-call persistence for the Twilio <Gather> voice pipeline.

Twilio's own status-callback signal turned out not to be reliable enough to be
the only path a transcript takes to Postgres: updating a live call's
`StatusCallback` via the REST API (`Call.update`) does not reliably fire for
that same call's later organic completion, and a caller who simply hangs up
mid-<Gather> triggers no further webhook at all — no `/voice/respond`, no
`/voice/status`. So finalization is called from every place that can know a
call is over:

  * `voice_respond`, the instant the app itself decides to hang up (the
    common case — most calls end because the agent says goodbye, not because
    Twilio tells us it ended);
  * `voice_status`, Twilio's own signal, when it does arrive;
  * `twilio_sync`'s periodic reconciliation, as a safety net for whatever
    slips through both of the above (e.g. the caller hangs up before the
    agent's closing line and no callback is configured for that number).

Idempotent by design — see `finalize_gather_call`.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CallDirection, CallLog, CallStatus
from app.services.call_state import CallStateStore
from app.services.caller_identity import persist_caller_identity
from app.services.grok_service import grok_service, primary_caller_language

logger = logging.getLogger(__name__)


async def finalize_gather_call(
    db: AsyncSession,
    state: CallStateStore,
    call_sid: str,
    *,
    status: CallStatus = CallStatus.COMPLETED,
    duration_seconds: int | None = None,
) -> bool:
    """End the Redis session for `call_sid` and write its transcript to `call_logs`.

    A `call_log` with both `ended_at` and `transcript` already set is treated
    as already finalized (by whichever caller got there first) and left
    untouched, so a duplicate Twilio callback or a reconciliation pass can't
    re-run analysis or clobber a field a previous finalize already wrote.

    Checking `transcript` rather than just `ended_at` matters: a call_log's
    `ended_at` can also be set by `twilio_sync`'s older, metadata-only merge
    (status/timing backfilled straight from Twilio, no Redis involved) without
    ever writing a transcript. Skipping on `ended_at` alone would let such a
    row look "finalized" forever with `transcript` permanently NULL. Returns
    whether this call actually performed the finalization.
    """
    call_log = (
        await db.execute(select(CallLog).where(CallLog.twilio_call_sid == call_sid))
    ).scalar_one_or_none()

    # Always end the Redis session, even if there is no call_log row (or it is
    # already finalized) — an orphaned "active" session that never gets ended
    # keeps failing the one invariant this module exists to guarantee.
    session = await state.end(call_sid)

    already_finalized = (
        call_log is not None
        and call_log.ended_at is not None
        and call_log.transcript is not None
    )
    if call_log is None or already_finalized:
        return False

    call_log.status = status
    call_log.ended_at = datetime.now(timezone.utc)
    call_log.duration_seconds = duration_seconds if duration_seconds is not None else (
        int((call_log.ended_at - call_log.started_at).total_seconds())
        if call_log.started_at
        else None
    )

    if session is None:
        await db.commit()
        logger.warning("call %s: finalized with no Redis session to pull a transcript from", call_sid)
        return True

    call_log.transcript = session.transcript_text or None
    call_log.ai_analysis = {
        **(call_log.ai_analysis or {}),
        "booking_outcome": session.booking_status
        if session.booking_status != "none"
        else "no_booking",
        "linked_appointment_id": session.appointment_id,
    }
    if session.history:
        analysis = await grok_service.analyze_call(session)
        if analysis:
            call_log.ai_summary = analysis.summary
            call_log.ai_analysis.update(analysis.model_dump())
            if call_log.direction is CallDirection.INBOUND:
                call_log.primary_language = primary_caller_language(
                    session, analysis.primary_language
                )
                await persist_caller_identity(
                    db, session, analysis.caller_name, analysis.caller_email
                )

    await db.commit()
    logger.info(
        "call %s: finalized via gather pipeline (%d chars of transcript)",
        call_sid,
        len(call_log.transcript or ""),
    )
    return True
