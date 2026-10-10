"""Caller name is required before a new booking, and card speech is split.

Pre-booking speech is the spa's card-on-file policy. Post-booking speech
mentions a secure text only when that text was actually sent.
"""
import pytest

from app.services.appointment_booking_service import BookingOutcome, confirm_booking
from app.services.booking_state import (
    CARD_LINK_PERMISSION_QUESTION,
    CARD_ON_FILE_HESITANT,
    CARD_ON_FILE_POLICY,
    CALLER_NAME_QUESTION,
    BookingDraft,
    booking_card_speech,
    confirmation_block_reason,
    get_draft,
    invalidate_booking_proposal,
    looks_card_hesitant,
    save_draft,
    stage,
    supplied_caller_name,
)
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent
from app.services.xai_realtime import XAIVoiceSession


def _session() -> CallSession:
    return CallSession(
        call_sid="CAname",
        direction="inbound",
        from_number="+15551212",
        to_number="+15550001",
        business_name="Test Spa",
        timezone="America/Chicago",
        tenant_id="11111111-1111-1111-1111-111111111111",
    )


def test_prebooking_policy_is_not_the_postbooking_sms_line():
    sent = booking_card_speech("pending_card", "sent")
    failed = booking_card_speech("pending_card", "failed")
    assert CARD_ON_FILE_POLICY not in sent
    assert "secure text link to add your card on file" in sent
    assert "You can complete it after this call." in sent
    assert "sent you" not in failed
    assert "wasn't able to send" in failed
    for status in ("not_required", "not_supported", "unknown", "card_confirmed", "failed"):
        assert booking_card_speech(status, "sent") == ""
        assert booking_card_speech(status, "not_attempted") == ""


def test_confirmation_speech_mentions_sms_only_when_it_was_sent():
    def confirmation(card_status: str, card_sms: str, booking_id: str) -> str:
        session = _session()
        session.selected_service = "Swedish massage"
        session.confirmed_datetime = "2026-10-02T16:15:00+00:00"
        session.timezone = "America/Chicago"
        voice = XAIVoiceSession("CAname", session)
        return voice._authoritative_tool_followup(
            "confirm_appointment",
            '{"status":"booked","appointment_id":"a","external_booking_id":"'
            + booking_id
            + '","card_status":"'
            + card_status
            + '","card_sms":"'
            + card_sms
            + '"}',
        )

    sent = confirmation("pending_card", "sent", "sent-booking")
    failed = confirmation("pending_card", "failed", "failed-booking")
    plain = confirmation("not_required", "not_attempted", "plain-booking")
    confirmed = confirmation("card_confirmed", "not_attempted", "confirmed-booking")

    # Re-delivery of the same provider success must never produce a second
    # spoken confirmation. This is separate from testing the four card states.
    duplicate_session = _session()
    duplicate_session.selected_service = "Swedish massage"
    duplicate_session.confirmed_datetime = "2026-10-02T16:15:00+00:00"
    duplicate_voice = XAIVoiceSession("CAname", duplicate_session)
    duplicate_payload = (
        '{"status":"booked","appointment_id":"a","external_booking_id":"duplicate",'
        '"card_status":"pending_card","card_sms":"sent"}'
    )
    first = duplicate_voice._authoritative_tool_followup(
        "confirm_appointment",
        duplicate_payload,
    )
    duplicate = duplicate_voice._authoritative_tool_followup(
        "confirm_appointment",
        duplicate_payload,
    )
    assert first
    assert duplicate == ""
    assert "reserved pending your card on file" in sent
    assert "is confirmed" not in sent
    assert "secure text link" in sent
    assert CARD_ON_FILE_POLICY not in sent
    assert "Is there anything else I can help you with today?" in sent
    assert "wasn't able to send" in failed
    assert "secure text link" not in failed
    assert "card" not in plain.lower()
    assert "card" not in confirmed.lower()


def test_hesitant_line_is_the_exact_policy_followup():
    assert looks_card_hesitant("Do I have to put a card down?")
    assert not looks_card_hesitant("yes")
    assert "24 hours" not in CARD_ON_FILE_HESITANT
    assert "24-hour" not in CARD_ON_FILE_POLICY
    assert "$39" not in CARD_ON_FILE_POLICY
    assert "configured policy" in CARD_ON_FILE_HESITANT
    assert "does not charge your card" in CARD_ON_FILE_POLICY


def test_missing_caller_name_blocks_final_confirmation():
    session = _session()
    session.booking_status = "awaiting_confirmation"
    draft = get_draft(session)
    draft.start_iso = "2026-10-03T16:15:00+00:00"
    draft.service_description = "Swedish massage"
    draft.selected_slot = {"start": draft.start_iso}
    draft.provider_verified = True
    save_draft(session, draft)

    assert confirmation_block_reason(session) == "caller_name_missing"
    assert supplied_caller_name(draft) is None


@pytest.mark.asyncio
async def test_confirm_without_a_name_does_not_create_a_booking():
    session = _session()
    session.booking_status = "awaiting_confirmation"
    draft = get_draft(session)
    draft.caller_name = None
    draft.service_description = "Swedish massage"
    save_draft(session, draft)

    result = await confirm_booking(object(), session)

    assert result.outcome is BookingOutcome.MISSING_INFO
    assert result.appointment is None
    assert "has not given their name" in result.message
    assert session.entities.get("awaiting_caller_name") is True
    assert session.booking_status == "collecting_details"


def test_name_already_on_the_draft_is_not_required_again():
    session = _session()
    stage(
        session,
        AppointmentIntent(
            intent="schedule",
            confidence=1.0,
            caller_name="Rayn",
            service_description="Swedish massage",
            requested_start_iso="2026-10-03T16:15:00",
        ),
    )
    assert confirmation_block_reason(session) != "caller_name_missing"
    assert get_draft(session).caller_name == "Rayn"
    assert CALLER_NAME_QUESTION


def test_caller_name_survives_time_service_and_provider_changes():
    session = _session()
    stage(
        session,
        AppointmentIntent(
            intent="schedule",
            confidence=1.0,
            caller_name="Rayn",
            service_description="HydroLux facial",
            preferred_staff="Ana",
            requested_start_iso="2026-10-03T16:15:00",
        ),
    )
    stage(
        session,
        AppointmentIntent(
            intent="schedule",
            confidence=1.0,
            service_description="Swedish massage",
            preferred_staff="Mia",
            requested_start_iso="2026-10-04T18:00:00",
        ),
    )
    draft = get_draft(session)
    assert draft.caller_name == "Rayn"
    assert draft.service_description == "Swedish massage"
    assert draft.preferred_staff == "Mia"
    assert draft.start_iso == "2026-10-04T18:00:00"
    invalidate_booking_proposal(session, "date_time")
    assert get_draft(session).caller_name == "Rayn"


def test_card_policy_blocks_confirmation_until_it_has_been_spoken():
    session = _session()
    session.booking_status = "awaiting_confirmation"
    session.entities["card_on_file_required"] = True
    draft = BookingDraft(
        caller_name="Rayn",
        service_description="Swedish massage",
        start_iso="2026-10-03T16:15:00+00:00",
        selected_slot={"start": "2026-10-03T16:15:00+00:00", "team_member_id": "tm"},
        provider_verified=True,
    )
    save_draft(session, draft)
    assert confirmation_block_reason(session) == "card_policy_not_explained"
    session.entities["card_policy_explained"] = True
    assert confirmation_block_reason(session) == "card_link_consent_missing"
    session.entities["card_link_consent_authorized_revision"] = draft.draft_revision
    assert confirmation_block_reason(session) != "card_link_consent_missing"


def test_card_policy_asks_permission_after_the_safety_warning():
    warning = "please don't read card details aloud"
    assert warning in CARD_ON_FILE_POLICY
    assert CARD_ON_FILE_POLICY.index(warning) < CARD_ON_FILE_POLICY.index(
        CARD_LINK_PERMISSION_QUESTION
    )


@pytest.mark.asyncio
async def test_voice_confirm_asks_for_a_missing_name_and_does_not_book():
    session = _session()
    session.booking_status = "awaiting_confirmation"
    draft = get_draft(session)
    draft.service_description = "Swedish massage"
    draft.start_iso = "2026-10-03T16:15:00+00:00"
    draft.selected_slot = {"start": draft.start_iso}
    draft.provider_verified = True
    draft.read_back = True
    save_draft(session, draft)
    voice = XAIVoiceSession("CAname", session)
    spoken = []

    async def _send(payload):
        spoken.append(payload)

    async def _cancel():
        return None

    voice._send = _send
    voice._cancel_active_response = _cancel

    handled = await voice._confirm_pending_booking_from_caller("yes")
    assert handled is True
    assert session.booking_status == "collecting_details"
    text = str(spoken)
    assert CALLER_NAME_QUESTION in text
    assert "external_booking" not in text
