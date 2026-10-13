from unittest.mock import AsyncMock
import pytest
from app.services.card_email_conversation import begin_email, email_answer, confirmed_email
from app.services.booking_state import get_draft, save_draft, CARD_ON_FILE_POLICY, booking_card_speech
from app.services.xai_realtime import XAIVoiceSession
from tests.test_caller_name_and_card_speech import _session


def test_address_needs_explicit_readback_acceptance():
    session = _session()
    begin_email(session)
    assert email_answer(session, "yes")[2] is False
    handled, line, ready = email_answer(session, "customer at gmail dot com")
    assert handled and not ready and "customer@gmail.com" in line
    assert confirmed_email(session) is None
    assert email_answer(session, "")[2] is False
    assert email_answer(session, "yes")[2] is True
    assert confirmed_email(session) == "customer@gmail.com"
    assert get_draft(session).caller_email == "customer@gmail.com"


def test_correction_and_revision_invalidate_email_acceptance():
    session = _session()
    begin_email(session)
    email_answer(session, "wrong@example.com")
    email_answer(session, "no")
    assert email_answer(session, "yes")[2] is False
    email_answer(session, "correct@example.com")
    email_answer(session, "yes")
    draft = get_draft(session)
    draft.draft_revision += 1
    save_draft(session, draft)
    assert confirmed_email(session) is None


@pytest.mark.asyncio
async def test_policy_collects_email_before_claiming_policy_explained():
    session = _session()
    session.entities["card_link_delivery"] = "email"
    draft = get_draft(session)
    draft.confirmation_authorized = True
    save_draft(session, draft)
    session.entities["caller_confirmed_revision"] = draft.draft_revision
    voice = XAIVoiceSession("CAemail", session)
    voice._send_force_message = AsyncMock()
    await voice._explain_card_policy()
    assert "email address" in voice._send_force_message.call_args.args[0]
    assert not session.entities.get("card_policy_explained")
    email_answer(session, "customer@example.com")
    email_answer(session, "yes")
    await voice._explain_card_policy()
    assert voice._send_force_message.call_args.args[0] == CARD_ON_FILE_POLICY


def test_email_confirmation_does_not_claim_delivery_or_sms():
    speech = booking_card_speech("pending_card", "email_accepted")
    assert "accepted for sending" in speech and "text link" not in speech
    assert "remains in place" in booking_card_speech("pending_card", "email_delivery_unknown")


def test_email_can_contain_day_or_service_words():
    session = _session()
    begin_email(session)
    handled, line, _ = email_answer(session, "friday.massage@example.com")
    assert handled and "friday.massage@example.com" in line


def test_unintelligible_email_offers_help_without_authorizing_callback():
    session = _session()
    begin_email(session)
    for _ in range(3):
        handled, line, ready = email_answer(session, "unclear")
    assert handled and not ready and "callback" in line
    assert session.entities.get("callback_offer_pending")
    assert not session.entities.get("callback_authorized")
    assert confirmed_email(session) is None


@pytest.mark.asyncio
async def test_real_force_message_email_keeps_voice_policy_playback_gate():
    session = _session()
    session.entities["card_link_delivery"] = "email"
    draft = get_draft(session)
    draft.confirmation_authorized = True
    save_draft(session, draft)
    session.entities["caller_confirmed_revision"] = draft.draft_revision
    begin_email(session)
    email_answer(session, "customer@example.com")
    email_answer(session, "yes")
    voice = XAIVoiceSession("CAemail", session)
    voice._send = AsyncMock()
    voice._localized_force_message = AsyncMock(side_effect=lambda text: text)
    await voice._send_force_message(CARD_ON_FILE_POLICY)
    output = voice._send.call_args.args[0]["item"]["content"][0]["text"]
    assert "email you" in output and "text you" not in output
    assert "emailing the secure card link" in output
    assert voice._card_policy_playback_revision == draft.draft_revision
    assert "card_link_consent_authorized_revision" not in session.entities
