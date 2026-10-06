"""One opening greeting per CallSid — never on tools, barge-in, or reconnect."""
from __future__ import annotations

import json
import uuid

import pytest

from app.services.call_state import (
    CALL_PHASE_ACTIVE,
    CALL_PHASE_GREETED,
    CALL_PHASE_GREETING_REQUESTED,
    CALL_PHASE_NEW,
    CallSession,
)
from app.services.xai_realtime import HOLD_ACK_TEXT, XAIVoiceSession


def _session(call_sid: str = "call-greet") -> CallSession:
    session = CallSession(
        call_sid=call_sid,
        direction="inbound",
        from_number="+14155550100",
        to_number="+18058878345",
        business_name="Healing Waters Wellness",
        tenant_id=str(uuid.uuid4()),
    )
    session.add_turn(
        "assistant",
        "Thank you for calling Healing Waters Wellness. How can I help you?",
    )
    return session


def _voice(monkeypatch, session: CallSession | None = None) -> tuple[XAIVoiceSession, list]:
    voice = XAIVoiceSession("call-id", session or _session())
    sent: list = []

    async def _capture(payload):
        sent.append(payload)

    async def _noop():
        return None

    async def _noop_cancel():
        if voice._active_response_id:
            voice._cancelled_response_ids.add(voice._active_response_id)
            voice._active_response_id = None
        return None

    monkeypatch.setattr(voice, "_send", _capture)
    monkeypatch.setattr(voice, "_persist_session", _noop)
    monkeypatch.setattr(voice, "_cancel_active_response", _noop_cancel)
    return voice, sent


def _greeting_payloads(sent: list) -> list:
    greetings = []
    for payload in sent:
        item = payload.get("item") or {}
        contents = item.get("content") or []
        text = " ".join(str(part.get("text") or "") for part in contents)
        if "thank you for calling" in text.lower() or "greet them" in text.lower():
            greetings.append(text)
    return greetings


async def _deliver_opening_audio(voice: XAIVoiceSession, response_id: str = "greet-1") -> None:
    await voice._dispatch({"type": "response.created", "response": {"id": response_id}})
    await voice._dispatch(
        {
            "type": "response.output_audio.delta",
            "response": {"id": response_id},
            "delta": "AA==",
        }
    )


@pytest.mark.asyncio
async def test_1_normal_multi_turn_call_greets_once(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    assert voice.session.greeting_requested is True
    assert voice.session.greeting_sent is False
    assert voice.session.phase == CALL_PHASE_GREETING_REQUESTED
    await _deliver_opening_audio(voice)
    assert voice.session.greeting_sent is True
    assert voice.session.phase == CALL_PHASE_GREETED

    for i in range(12):
        await voice._dispatch(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "data": {"transcript": f"I would like a facial on day {i}"},
            }
        )
        await voice._dispatch(
            {
                "type": "response.output_audio_transcript.done",
                "data": {"transcript": f"Sure, I can help with that request {i}."},
            }
        )
        await voice._dispatch({"type": "response.created", "response": {"id": f"resp-{i}"}})
        await voice._dispatch({"type": "response.done", "response": {"id": f"resp-{i}"}})

    assert len(_greeting_payloads(sent)) == 1
    assert voice.session.phase == CALL_PHASE_ACTIVE
    assert voice.session.greeting_sent is True


@pytest.mark.asyncio
async def test_2_tool_call_does_not_regreet(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    await _deliver_opening_audio(voice)
    before = len(_greeting_payloads(sent))
    await voice._dispatch(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "data": {"transcript": "Where are you located?"},
        }
    )
    await voice._send_function_output(
        "fc_loc",
        json.dumps({"status": "ok", "message": "We are at 123 Main Street."}),
        nudge=True,
    )
    assert len(_greeting_payloads(sent)) == before


@pytest.mark.asyncio
async def test_3_availability_followup_does_not_regreet(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    await _deliver_opening_audio(voice)
    before = len(_greeting_payloads(sent))
    await voice._dispatch(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "data": {"transcript": "Can you check 4 PM?"},
        }
    )
    await voice._dispatch({"type": "response.created", "response": {"id": "resp-av"}})
    await voice._dispatch({"type": "response.done", "response": {"id": "resp-av"}})
    assert len(_greeting_payloads(sent)) == before


@pytest.mark.asyncio
async def test_4_hold_ack_is_not_a_greeting(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    await _deliver_opening_audio(voice)
    before = len(_greeting_payloads(sent))
    voice.HOLD_ACK_DELAY_SECONDS = 0
    voice._hold_ack_played_this_turn = False
    voice._ws = object()
    await voice._maybe_hold_ack()
    texts = [
        str(((p.get("item") or {}).get("content") or [{}])[0].get("text") or "")
        for p in sent
    ]
    assert HOLD_ACK_TEXT in texts
    assert HOLD_ACK_TEXT == "Let me check that for you."
    assert "thank you for calling" not in HOLD_ACK_TEXT.lower()
    assert len(_greeting_payloads(sent)) == before


@pytest.mark.asyncio
async def test_5_barge_in_does_not_reset_greeting(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    await _deliver_opening_audio(voice)
    before = len(_greeting_payloads(sent))
    for i in range(4):
        await voice._dispatch({"type": "input_audio_buffer.speech_started"})
        await voice._dispatch(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "data": {"transcript": f"Actually make it Friday {i}"},
            }
        )
    assert voice.session.greeting_sent is True
    assert len(_greeting_payloads(sent)) == before


@pytest.mark.asyncio
async def test_6_response_cancelled_does_not_regreet(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    await _deliver_opening_audio(voice)
    before = len(_greeting_payloads(sent))
    await voice._dispatch({"type": "response.created", "response": {"id": "resp-cancel-me"}})
    await voice._cancel_active_response()
    await voice._dispatch(
        {
            "type": "response.output_audio_transcript.done",
            "data": {"transcript": "Friday is open at 2 PM."},
        }
    )
    assert len(_greeting_payloads(sent)) == before
    assert any("Friday is open" in str(t.get("content")) for t in voice.session.history if t["role"] == "assistant")


@pytest.mark.asyncio
async def test_7_tool_continuation_after_response_done_is_not_a_greeting(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    await _deliver_opening_audio(voice)
    before = len(_greeting_payloads(sent))
    voice._response_needed_after_tool = True
    await voice._dispatch({"type": "response.created", "response": {"id": "resp-tool"}})
    await voice._dispatch({"type": "response.done", "response": {"id": "resp-tool"}})
    creates = [p for p in sent if p.get("type") == "response.create"]
    assert creates
    assert len(_greeting_payloads(sent)) == before
    assert not any("Greet them" in json.dumps(p) for p in sent)


@pytest.mark.asyncio
async def test_8_websocket_reconnect_keeps_greeting_sent(monkeypatch):
    session = _session("CA-live")
    first, sent1 = _voice(monkeypatch, session)
    await first._greet()
    await _deliver_opening_audio(first)
    assert session.greeting_sent is True
    assert session.phase == CALL_PHASE_GREETED

    reconnect, sent2 = _voice(monkeypatch, session)
    assert reconnect._greeted is True
    await reconnect._greet()
    assert sent2 == []
    assert session.greeting_sent is True


@pytest.mark.asyncio
async def test_9_new_callsid_gets_its_own_greeting(monkeypatch):
    first, sent1 = _voice(monkeypatch, _session("CA-old"))
    await first._greet()
    await _deliver_opening_audio(first)
    first.session.greeting_sent = True

    second, sent2 = _voice(monkeypatch, _session("CA-new"))
    assert second.session.greeting_sent is False
    assert second.session.phase == CALL_PHASE_NEW
    await second._greet()
    assert len(_greeting_payloads(sent2)) == 1
    await _deliver_opening_audio(second)
    assert second.session.greeting_sent is True


@pytest.mark.asyncio
async def test_model_restart_greeting_is_cancelled_mid_call(monkeypatch):
    voice, sent = _voice(monkeypatch)
    await voice._greet()
    await _deliver_opening_audio(voice)
    voice._user_turn_count = 4
    await voice._dispatch(
        {
            "type": "response.output_audio_transcript.delta",
            "data": {
                "delta": "Hi, thank you for calling Healing Waters Wellness. How can I help you today?"
            },
        }
    )
    assert voice._pending_agent == ""
    assistant = [t for t in voice.session.history if t["role"] == "assistant"]
    assert all("how can i help you today" not in t["content"].lower() for t in assistant[1:])
