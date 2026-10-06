"""Opening greeting must play automatically, exactly once, only after outbound ready."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services.call_state import (
    CALL_PHASE_GREETED,
    CALL_PHASE_GREETING_REQUESTED,
    CALL_PHASE_NEW,
    CallSession,
)
from app.services.media_bridge import TwilioMediaBridge
from app.services.xai_realtime import XAIVoiceSession
from tests.test_greeting_once_per_call import _deliver_opening_audio, _greeting_payloads, _session, _voice
from tests.test_media_bridge import _FakeTwilioWS


def _bridge(monkeypatch, stream_sid: str | None = "MZ-ready") -> TwilioMediaBridge:
    twilio = _FakeTwilioWS()
    session = CallSession(
        call_sid="CAbridge",
        direction="inbound",
        from_number="+12149702434",
        to_number="+18058878345",
        business_name="Healing Waters Day Spa",
        tenant_id=str(uuid.uuid4()),
    )
    bridge = TwilioMediaBridge("CAbridge", session, twilio)
    bridge._stream_sid = stream_sid

    sent: list = []

    async def _capture(payload):
        sent.append(payload)

    async def _noop():
        return None

    monkeypatch.setattr(bridge, "_send", _capture)
    monkeypatch.setattr(bridge, "_persist_session", _noop)
    bridge.xai_sent = sent  # type: ignore[attr-defined]
    return bridge


@pytest.mark.asyncio
async def test_1_silent_caller_still_gets_one_automatic_greeting(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    assert voice.session.greeting_requested is True
    assert voice.session.greeting_sent is False
    await _deliver_opening_audio(voice)
    assert voice.session.greeting_sent is True
    assert voice.session.phase == CALL_PHASE_GREETED
    assert len(_greeting_payloads(sent)) == 1
    await asyncio.sleep(0.05)
    assert len(_greeting_payloads(sent)) == 1


@pytest.mark.asyncio
async def test_2_stream_not_ready_does_not_fire_or_drop_greeting(monkeypatch):
    bridge = _bridge(monkeypatch, stream_sid=None)
    await bridge._greet()
    assert bridge.xai_sent == []
    assert bridge.session.greeting_requested is False
    assert bridge.session.greeting_sent is False
    assert bridge.session.phase == CALL_PHASE_NEW

    bridge._stream_sid = "MZ-late"
    bridge._playback_task = object()  # type: ignore[assignment]
    await bridge._greet()
    assert len(_greeting_payloads(bridge.xai_sent)) == 1
    assert bridge.session.greeting_requested is True
    assert bridge.session.greeting_sent is False
    await bridge._dispatch(
        {
            "type": "response.output_audio.delta",
            "response": {"id": "greet-1"},
            "delta": "AA==",
        }
    )
    assert bridge.session.greeting_sent is True


@pytest.mark.asyncio
async def test_3_force_message_audio_marks_greeting_sent(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    assert voice.session.greeting_requested is True
    assert voice.session.greeting_sent is False
    await _deliver_opening_audio(voice)
    assert voice.session.greeting_sent is True
    assert voice.session.greeting_audio_started is True
    assert len(_greeting_payloads(sent)) == 1


@pytest.mark.asyncio
async def test_4_force_message_no_audio_runs_startup_fallback_once(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    await voice._dispatch({"type": "response.created", "response": {"id": "greet-empty"}})
    await voice._dispatch({"type": "response.done", "response": {"id": "greet-empty"}})
    assert voice.session.greeting_sent is False
    assert voice._greeting_fallback_used is True
    assert any(
        ((p.get("item") or {}).get("content") or [{}])[0].get("text")
        == "[The caller has just connected. Greet them.]"
        for p in sent
    )
    await voice._dispatch({"type": "response.created", "response": {"id": "greet-fb"}})
    await voice._dispatch(
        {
            "type": "response.output_audio.delta",
            "response": {"id": "greet-fb"},
            "delta": "AA==",
        }
    )
    assert voice.session.greeting_sent is True
    assert sum(1 for p in sent if p.get("type") == "response.create") == 1


@pytest.mark.asyncio
async def test_5_force_message_error_falls_back_once(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    await voice._dispatch({"type": "error", "data": {"message": "unknown item type"}})
    assert voice.session.greeting_sent is False
    assert voice._greeting_fallback_used is True
    await _deliver_opening_audio(voice, "greet-fb")
    assert voice.session.greeting_sent is True
    await voice._dispatch({"type": "error", "data": {"message": "again"}})
    assert sum(1 for p in sent if p.get("type") == "response.create") == 1


@pytest.mark.asyncio
async def test_6_midcall_error_does_not_fallback_greeting(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    await _deliver_opening_audio(voice)
    before = len(sent)
    voice._user_turn_count = 3
    await voice._dispatch({"type": "error", "data": {"message": "boom"}})
    assert sent[before:] == []
    assert voice.session.greeting_sent is True


@pytest.mark.asyncio
async def test_7_caller_hello_during_startup_does_not_second_greet(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    await voice._dispatch({"type": "input_audio_buffer.speech_started"})
    await voice._dispatch(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "data": {"transcript": "Hello?"},
        }
    )
    await _deliver_opening_audio(voice)
    assert len(_greeting_payloads(sent)) == 1
    assert voice.session.greeting_sent is True
    await voice._greet()
    assert len(_greeting_payloads(sent)) == 1


@pytest.mark.asyncio
async def test_8_barge_in_keeps_greeting_sent(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    await _deliver_opening_audio(voice)
    assert voice.session.greeting_sent is True
    before = len(_greeting_payloads(sent))
    await voice._dispatch({"type": "input_audio_buffer.speech_started"})
    await voice._dispatch(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "data": {"transcript": "Can I book a massage?"},
        }
    )
    assert voice.session.greeting_sent is True
    assert len(_greeting_payloads(sent)) == before


@pytest.mark.asyncio
async def test_9_new_callsid_gets_its_own_automatic_greeting(monkeypatch):
    first, _ = _voice(monkeypatch, _session("CA-old"))
    await first._greet()
    await _deliver_opening_audio(first)
    second, sent2 = _voice(monkeypatch, _session("CA-new"))
    assert second.session.phase == CALL_PHASE_NEW
    await second._greet()
    assert second.session.phase == CALL_PHASE_GREETING_REQUESTED
    await _deliver_opening_audio(second)
    assert second.session.greeting_sent is True
    assert len(_greeting_payloads(sent2)) == 1
