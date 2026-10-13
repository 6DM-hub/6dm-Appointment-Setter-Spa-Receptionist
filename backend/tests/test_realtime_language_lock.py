"""Regression coverage for one-language-per-call realtime speech."""
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from app.services.booking_state import get_draft, is_affirmative, is_negative, save_draft
from app.services.call_language import (
    detect_requested_call_language,
    normalize_call_language,
)
from app.services.call_state import CallSession
from app.services.xai_realtime import (
    SET_CONVERSATION_LANGUAGE_TOOL,
    VOICE_TOOLS,
    XAIVoiceSession,
)


def _voice() -> XAIVoiceSession:
    session = CallSession(
        call_sid="call-language",
        direction="inbound",
        from_number="+12145550101",
        to_number="+12145550102",
        tenant_id=str(uuid.uuid4()),
        business_name="Your Day Spa",
        timezone="America/Chicago",
    )
    session.add_turn("assistant", "Thank you for calling Your Day Spa.")
    return XAIVoiceSession("call-language", session)


@pytest.mark.parametrize(
    "utterance,code",
    [
        ("¿Podemos hablar en español?", "es-MX"),
        ("Quiero reservar una cita para un masaje mañana", "es-MX"),
        ("Je voudrais un rendez-vous pour un massage demain", "fr"),
        ("明天下午我想预约按摩", "zh"),
        ("日本語で話してください", "ja"),
        ("Я хочу записаться на массаж", "ru"),
    ],
)
def test_detects_explicit_or_substantial_non_english_turn(utterance, code):
    assert detect_requested_call_language(utterance).code == code


@pytest.mark.parametrize(
    "utterance",
    [
        "Spanish massage with Maria",
        "Yes",
        "SIX",
        "Swedish Massage at 3 PM",
    ],
)
def test_does_not_lock_on_isolated_names_or_menu_words(utterance):
    assert detect_requested_call_language(utterance) is None


def test_language_tool_is_available_to_realtime_model():
    assert SET_CONVERSATION_LANGUAGE_TOOL in VOICE_TOOLS
    assert "es-MX" in SET_CONVERSATION_LANGUAGE_TOOL["parameters"]["properties"]["language_code"]["enum"]


async def test_caller_language_locks_session_updates_asr_and_preserves_ara(monkeypatch):
    voice = _voice()
    sent = []
    voice._ws = object()
    voice._realtime_instructions = "base booking instructions"

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice, "_send", capture)
    monkeypatch.setattr(voice, "_persist_session", AsyncMock())

    await voice._dispatch({
        "type": "conversation.item.input_audio_transcription.completed",
        "data": {"transcript": "Quiero reservar una cita para un masaje mañana"},
    })

    state = voice.session.entities["conversation_language"]
    assert state == {
        "name": "Spanish",
        "code": "es-MX",
        "locked": True,
        "source": "caller_transcript",
    }
    update = next(item for item in sent if item["type"] == "session.update")
    assert "Speak only Spanish" in update["session"]["instructions"]
    assert update["session"]["audio"]["input"]["transcription"]["language_hint"] == "es-MX"
    assert "voice" not in update["session"], "mid-call lock must not replace Ara"

    # Later French does not mutate the per-call language lock.
    await voice._dispatch({
        "type": "conversation.item.input_audio_transcription.completed",
        "data": {"transcript": "Je voudrais parler français"},
    })
    assert voice.session.entities["conversation_language"]["code"] == "es-MX"
    assert sum(item["type"] == "session.update" for item in sent) == 1


async def test_language_tool_is_idempotent_for_duration_of_call(monkeypatch):
    voice = _voice()
    sent = []
    voice._ws = object()
    voice._realtime_instructions = "base"
    monkeypatch.setattr(voice, "_send", AsyncMock(side_effect=lambda payload: sent.append(payload)))

    first = json.loads(await voice._run_set_conversation_language('{"language_code":"es-MX"}'))
    second = json.loads(await voice._run_set_conversation_language('{"language_code":"fr"}'))

    assert first["status"] == "locked"
    assert second["status"] == "already_locked"
    assert second["language_code"] == "es-MX"
    assert sum(item["type"] == "session.update" for item in sent) == 1


async def test_force_messages_are_translated_without_changing_booking_facts(monkeypatch):
    from app.services import xai_realtime

    voice = _voice()
    voice.session.entities["conversation_language"] = {
        "name": "Spanish", "code": "es-MX", "locked": True,
    }
    draft = get_draft(voice.session)
    draft.service_description = "60-minute Swedish Massage"
    draft.preferred_staff = "SIX"
    save_draft(voice.session, draft)
    sent = []
    monkeypatch.setattr(voice, "_send", AsyncMock(side_effect=lambda payload: sent.append(payload)))

    async def translate(masked, language):
        assert language == "Spanish"
        assert "Your Day Spa" not in masked
        assert "60-minute Swedish Massage" not in masked
        assert "SIX" not in masked
        # Preserve backend placeholders and numeric facts exactly.
        return masked.replace("is confirmed for", "está confirmada para").replace(
            "with", "con"
        )

    monkeypatch.setattr(xai_realtime.grok_service, "translate_voice_message", translate)
    await voice._send_force_message(
        "Your Day Spa: your 60-minute Swedish Massage with SIX is confirmed for 2:30 PM."
    )

    spoken = sent[-1]["item"]["content"][0]["text"]
    assert "Your Day Spa" in spoken
    assert "60-minute Swedish Massage" in spoken
    assert "SIX" in spoken
    assert "2:30 PM" in spoken
    assert "está confirmada para" in spoken


async def test_translation_failure_keeps_authoritative_booking_result(monkeypatch):
    from app.services import xai_realtime

    voice = _voice()
    voice.session.entities["conversation_language"] = {
        "name": "Spanish", "code": "es-MX", "locked": True,
    }
    sent = []
    monkeypatch.setattr(voice, "_send", AsyncMock(side_effect=lambda payload: sent.append(payload)))
    monkeypatch.setattr(
        xai_realtime.grok_service,
        "translate_voice_message",
        AsyncMock(return_value="Confirmada para las 3:00"),
    )

    source = "Your appointment is confirmed for 2:30 PM."
    await voice._send_force_message(source)
    assert sent[-1]["item"]["content"][0]["text"] == source


def test_language_names_and_codes_normalize_to_supported_bcp47():
    assert normalize_call_language("Spanish").code == "es-MX"
    assert normalize_call_language("español").code == "es-MX"
    assert normalize_call_language("pt-BR").name == "Portuguese"


@pytest.mark.parametrize("answer", ["sí", "oui", "sim", "ja", "はい", "네", "да"])
def test_short_unambiguous_confirmation_works_in_locked_languages(answer):
    assert is_affirmative(answer)


@pytest.mark.parametrize(
    "answer",
    ["sí, pero mañana", "oui, mais demain", "sim, mas amanhã", "ja, aber morgen"],
)
def test_foreign_language_confirmation_with_changes_never_consents(answer):
    assert not is_affirmative(answer)


@pytest.mark.parametrize("answer", ["no gracias", "non merci", "não", "nein danke", "いいえ", "아니요"])
def test_short_unambiguous_refusal_works_in_locked_languages(answer):
    assert is_negative(answer)
