"""Regression tests for realtime response and task lifecycle races.

These cases came from live calls where Square had already returned verified
availability, but a false VAD event or a late ``response.done`` could strand
the result or start a second, generic model response.  Teardown also must not
leave call-scoped work running after the transcript has been finalized.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import xai_realtime as realtime
from app.services.call_state import CallSession
from app.services.media_bridge import TwilioMediaBridge
from app.services.xai_realtime import XAIVoiceSession


def _session(call_id: str = "CAlifecycle") -> CallSession:
    return CallSession(
        call_sid=call_id,
        direction="inbound",
        from_number="+15550000001",
        to_number="+15550000002",
        business_name="Healing Waters Day Spa",
        timezone="America/Chicago",
    )


class _TwilioSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, _text: str) -> None:
        return None


class _Store:
    def __init__(self) -> None:
        self.save = AsyncMock()
        self.end = AsyncMock()


class _NoCallLogResult:
    def scalar_one_or_none(self):
        return None


class _NoCallLogDB:
    async def execute(self, _query):
        return _NoCallLogResult()


class _NoCallLogContext:
    async def __aenter__(self):
        return _NoCallLogDB()

    async def __aexit__(self, *_args):
        return None


async def test_false_vad_without_transcript_does_not_discard_verified_availability():
    """A noise-only VAD start must not erase a completed provider result.

    The caller may generate a ``speech_started`` event without any transcript.
    The buffered Square result must survive and be delivered when the hold
    response closes instead of leaving the line silent indefinitely.
    """
    session = _session()
    session.greeting_sent = True
    bridge = TwilioMediaBridge(session.call_sid, session, _TwilioSocket())
    bridge._stream_sid = "MZlifecycle"
    bridge._persist_session = AsyncMock()
    bridge._send = AsyncMock()
    bridge._speak_availability = AsyncMock()
    bridge._active_response_id = "hold-response"
    bridge._availability_hold_response_id = "hold-response"
    bridge._pending_availability_speech = "Three PM is available."

    await bridge._dispatch({"type": "input_audio_buffer.speech_started"})
    await bridge._dispatch(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "data": {"transcript": ""},
        }
    )

    assert bridge._pending_availability_speech == "Three PM is available."
    assert bridge._availability_speech_interrupted is False

    await bridge._dispatch(
        {"type": "response.done", "response": {"id": "hold-response"}}
    )

    bridge._speak_availability.assert_awaited_once_with(
        "Three PM is available."
    )


async def test_finalize_settles_watchdog_and_inflight_tool_tasks(monkeypatch):
    """No call-owned task may continue after finalization has returned."""
    voice = XAIVoiceSession("CAlifecycle", _session())
    store = _Store()
    voice._store = lambda: store
    monkeypatch.setattr(realtime, "AsyncSessionLocal", lambda: _NoCallLogContext())

    watchdog_started = asyncio.Event()
    watchdog_settled = asyncio.Event()
    tool_release = asyncio.Event()
    tool_settled = asyncio.Event()

    async def watchdog():
        watchdog_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            watchdog_settled.set()

    async def inflight_tool():
        try:
            await tool_release.wait()
        finally:
            tool_settled.set()

    watchdog_task = asyncio.create_task(watchdog())
    tool_task = asyncio.create_task(inflight_tool())
    voice._availability_hold_watchdog_task = watchdog_task
    voice._inflight_tools.add(tool_task)
    tool_task.add_done_callback(voice._function_call_task_done)
    await watchdog_started.wait()

    finalize_task = asyncio.create_task(voice._finalize())
    try:
        await asyncio.sleep(0.02)

        # Implementations may either cancel an unfinished tool or await it.
        # Returning while it is still running is the forbidden behavior.
        assert watchdog_task.done()
        assert watchdog_settled.is_set()
        assert not finalize_task.done() or tool_task.done()

        tool_release.set()
        await asyncio.wait_for(finalize_task, timeout=0.5)

        assert tool_task.done()
        assert tool_settled.is_set()
        assert not voice._inflight_tools
        assert voice._availability_hold_watchdog_task is None
    finally:
        tool_release.set()
        for task in (watchdog_task, tool_task, finalize_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            watchdog_task, tool_task, finalize_task, return_exceptions=True
        )


async def test_late_done_for_cancelled_hold_cannot_start_generic_continuation():
    """The canceled hold's late completion cannot overlap verified speech."""
    voice = XAIVoiceSession("CAlifecycle", _session())
    voice._send = AsyncMock()
    voice._speak_availability = AsyncMock()
    voice._active_response_id = "hold-response"
    voice._availability_hold_response_id = "hold-response"
    voice._pending_availability_speech = "Three PM is available."
    voice._response_needed_after_tool = True

    released = await voice._release_pending_availability(
        reason="hold_response_timeout"
    )
    assert released is True
    voice._speak_availability.assert_awaited_once_with(
        "Three PM is available."
    )

    await voice._dispatch(
        {"type": "response.done", "response": {"id": "hold-response"}}
    )

    sent_payloads = [call.args[0] for call in voice._send.await_args_list]
    assert {"type": "response.cancel"} in sent_payloads
    assert not any(
        payload.get("type") == "response.create" for payload in sent_payloads
    )

