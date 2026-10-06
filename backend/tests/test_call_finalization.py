"""Shared end-of-call persistence for the <Gather> voice pipeline.

The invariant this module exists to guarantee: a call that has ended must not
be left with an active Redis session and a NULL transcript. That failure mode
is exactly what shipped to production — Twilio's own status-callback signal
never fired (`Call.update(status_callback=...)` does not reliably attach to a
call's own later completion, and a caller hanging up mid-<Gather> triggers no
webhook at all), so `voice_status` — the only place that wrote
`call_log.transcript` — never ran.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from app.models import CallDirection, CallLog, CallStatus
from app.services.call_finalization import finalize_gather_call
from app.services.call_state import CallSession
from app.services import call_finalization as call_finalization_module


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


def _call_log(**overrides) -> CallLog:
    defaults = dict(
        id=uuid.uuid4(),
        twilio_call_sid="CA-test",
        direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS,
        from_number="+15551234567",
        to_number="+15550000001",
        started_at=datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc),
        ended_at=None,
        ai_analysis={},
    )
    defaults.update(overrides)
    return CallLog(**defaults)


def _session_with_history(call_sid: str = "CA-test") -> CallSession:
    session = CallSession(call_sid, "inbound", "+15551234567", "+15550000001")
    session.add_turn("assistant", "Thank you for calling. How can I help?")
    session.add_turn("user", "I'd like to book a massage tomorrow at 2pm.")
    session.add_turn("assistant", "You're all set for tomorrow at 2pm. Goodbye!")
    return session


@pytest.mark.asyncio
async def test_finalize_writes_transcript_and_ends_redis_session(monkeypatch):
    call_log = _call_log()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result(call_log))

    session = _session_with_history()
    state = AsyncMock()
    state.end = AsyncMock(return_value=session)

    monkeypatch.setattr(
        call_finalization_module.grok_service, "analyze_call", AsyncMock(return_value=None)
    )

    finalized = await finalize_gather_call(db, state, "CA-test")

    assert finalized is True
    state.end.assert_awaited_once_with("CA-test")
    assert call_log.status is CallStatus.COMPLETED
    assert call_log.ended_at is not None
    assert call_log.duration_seconds == int(
        (call_log.ended_at - call_log.started_at).total_seconds()
    )
    assert call_log.transcript is not None
    assert "book a massage" in call_log.transcript
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_finalize_is_a_noop_when_already_finalized(monkeypatch):
    """A call_log with `ended_at` already set was finalized by whoever got
    there first (voice_respond's own hangup path, most commonly). A later
    Twilio status callback or a reconciliation sweep must not re-run
    analysis or overwrite the transcript that's already there."""
    call_log = _call_log(
        ended_at=datetime(2026, 9, 10, 16, 5, tzinfo=timezone.utc),
        transcript="Agent: goodbye",
        status=CallStatus.COMPLETED,
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result(call_log))
    state = AsyncMock()
    state.end = AsyncMock(return_value=_session_with_history())
    analyze = AsyncMock()
    monkeypatch.setattr(call_finalization_module.grok_service, "analyze_call", analyze)

    finalized = await finalize_gather_call(db, state, "CA-test")

    assert finalized is False
    # Redis is still cleaned up even though the DB row needed no changes.
    state.end.assert_awaited_once_with("CA-test")
    analyze.assert_not_awaited()
    assert call_log.transcript == "Agent: goodbye"


@pytest.mark.asyncio
async def test_finalize_retries_a_row_with_ended_at_but_no_transcript(monkeypatch):
    """Regression: a row can have `ended_at` set by twilio_sync's older,
    metadata-only merge (backfilled straight from Twilio, no Redis involved)
    without ever getting a transcript. Guarding the idempotency check on
    `ended_at` alone left such a row "finalized" forever with `transcript`
    permanently NULL — this is exactly what happened to a production call.
    Guarding on `transcript` too means it keeps trying."""
    call_log = _call_log(
        ended_at=datetime(2026, 9, 10, 16, 5, tzinfo=timezone.utc),
        transcript=None,
        status=CallStatus.COMPLETED,
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result(call_log))
    session = _session_with_history()
    state = AsyncMock()
    state.end = AsyncMock(return_value=session)
    monkeypatch.setattr(
        call_finalization_module.grok_service, "analyze_call", AsyncMock(return_value=None)
    )

    finalized = await finalize_gather_call(db, state, "CA-test")

    assert finalized is True
    assert call_log.transcript is not None
    assert "book a massage" in call_log.transcript


@pytest.mark.asyncio
async def test_finalize_ends_redis_session_even_without_a_call_log_row():
    """No call_logs row (e.g. an unattributed call) must still not leak an
    active Redis session forever."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result(None))
    state = AsyncMock()
    state.end = AsyncMock(return_value=_session_with_history())

    finalized = await finalize_gather_call(db, state, "CA-unowned")

    assert finalized is False
    state.end.assert_awaited_once_with("CA-unowned")


@pytest.mark.asyncio
async def test_finalize_marks_completed_even_if_redis_session_already_expired(monkeypatch):
    """Redis' 300s post-call grace period can lapse before this runs (e.g. a
    slow reconciliation pass); the call_log must still be closed out rather
    than left `in_progress` forever, just without a transcript to recover."""
    call_log = _call_log()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result(call_log))
    state = AsyncMock()
    state.end = AsyncMock(return_value=None)

    finalized = await finalize_gather_call(db, state, "CA-test", status=CallStatus.NO_ANSWER)

    assert finalized is True
    assert call_log.status is CallStatus.NO_ANSWER
    assert call_log.ended_at is not None
    assert call_log.transcript is None
