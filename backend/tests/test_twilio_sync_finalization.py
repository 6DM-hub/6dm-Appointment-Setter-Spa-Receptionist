"""twilio_sync's reconciliation safety net for calls the live flow never finalized.

`voice_respond` (own-hangup) and `voice_status` (Twilio's callback) are the
primary ways a call gets finalized; this periodic sync is the backstop for
whatever slips through both — a caller who hangs up mid-<Gather> with no
status callback configured for that number leaves no webhook at all. See
`app.services.call_finalization` for why that gap exists.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from app.models import CallDirection, CallLog, CallStatus
from app.services import twilio_sync
from app.services import twilio_service as twilio_service_module


class _TwilioCall:
    def __init__(self, sid, status="completed"):
        self.sid = sid
        self.direction = "inbound"
        self.status = status
        self._from = "+15551234567"
        self.to = "+15550000001"
        self.forwarded_from = None
        self.start_time = None
        self.end_time = None
        self.duration = "42"


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self._rows


def _row(
    sid: str,
    *,
    ended_at=None,
    status=CallStatus.IN_PROGRESS,
    duration_seconds=None,
    transcript=None,
) -> CallLog:
    return CallLog(
        id=uuid.uuid4(),
        twilio_call_sid=sid,
        direction=CallDirection.INBOUND,
        status=status,
        from_number="+15551234567",
        to_number="+15550000001",
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        transcript=transcript,
    )


@pytest.mark.asyncio
async def test_terminal_call_never_finalized_by_the_app_is_swept_up(monkeypatch):
    """The scenario that shipped to production: Twilio reports the call
    completed, but nothing in the app ever wrote a transcript (`ended_at` is
    still None). The sync must call the shared finalizer to recover it."""
    row = _row("CA-orphaned")
    monkeypatch.setattr(
        twilio_service_module.twilio_service,
        "list_recent_calls",
        Mock(return_value=[_TwilioCall("CA-orphaned")]),
    )
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(twilio_sync, "finalize_gather_call", finalize)
    monkeypatch.setattr(twilio_sync, "CallStateStore", Mock(return_value="fake-state"))

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarsResult([row]))

    report = await twilio_sync.sync_calls(db)

    finalize.assert_awaited_once()
    args, kwargs = finalize.call_args
    assert args[0] is db
    assert args[2] == "CA-orphaned"
    assert kwargs["status"] is CallStatus.COMPLETED
    assert report.updated == 1


@pytest.mark.asyncio
async def test_already_finalized_call_is_left_alone(monkeypatch):
    """A row the live flow already finalized (`ended_at` AND `transcript` set)
    must not be re-finalized — that would re-run Grok analysis and risk
    overwriting a transcript with a stale reconstruction."""
    row = _row(
        "CA-done",
        ended_at=datetime(2026, 9, 10, 16, 5, tzinfo=timezone.utc),
        status=CallStatus.COMPLETED,
        duration_seconds=42,
        transcript="Agent: goodbye",
    )
    monkeypatch.setattr(
        twilio_service_module.twilio_service,
        "list_recent_calls",
        Mock(return_value=[_TwilioCall("CA-done")]),
    )
    finalize = AsyncMock()
    monkeypatch.setattr(twilio_sync, "finalize_gather_call", finalize)

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarsResult([row]))

    report = await twilio_sync.sync_calls(db)

    finalize.assert_not_awaited()
    assert report.unchanged == 1


@pytest.mark.asyncio
async def test_row_with_ended_at_but_no_transcript_is_retried(monkeypatch):
    """Regression: exactly what happened in production. A row's `ended_at`
    got set by this same metadata-only merge on an earlier tick (before any
    finalize logic existed), so it looks "done" — but `transcript` is still
    NULL. Triggering off `ended_at` alone would leave it stuck like that
    forever; triggering off `transcript` keeps retrying it."""
    row = _row(
        "CA-poisoned",
        ended_at=datetime(2026, 9, 10, 16, 5, tzinfo=timezone.utc),
        status=CallStatus.COMPLETED,
        duration_seconds=0,
        transcript=None,
    )
    monkeypatch.setattr(
        twilio_service_module.twilio_service,
        "list_recent_calls",
        Mock(return_value=[_TwilioCall("CA-poisoned")]),
    )
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(twilio_sync, "finalize_gather_call", finalize)
    monkeypatch.setattr(twilio_sync, "CallStateStore", Mock(return_value="fake-state"))

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarsResult([row]))

    await twilio_sync.sync_calls(db)

    finalize.assert_awaited_once()
    assert finalize.call_args.args[2] == "CA-poisoned"
