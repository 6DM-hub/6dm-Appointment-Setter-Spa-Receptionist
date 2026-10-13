import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.spa_account import SpaPaymentPolicy
from app.services.appointment_booking_service import BookingOutcome
from app.services.call_state import CallSession
from app.services.grok_service import build_spa_prompt_context
from app.services.gratuity import (
    DEFAULT_GRATUITY_POLICY,
    fee_may_be_spoken,
    gratuity_policy_of,
    gratuity_question_answer,
    reminder_message,
)
from app.services.spa_facts import lookup_spa_facts
from tests.conftest import make_spa
from tests.test_confirmation_pause import pending_voice


def verified_policy(**overrides):
    return {
        **DEFAULT_GRATUITY_POLICY,
        "enabled": True,
        "fee_verified": True,
        "use_voice_personality": False,
        **overrides,
    }


def test_existing_businesses_default_to_disabled_gratuity_reminders():
    policy = SpaPaymentPolicy()
    assert policy.gratuity.enabled is False
    assert policy.gratuity.card_processing_fee_percent == 4
    assert reminder_message(policy.gratuity.model_dump(), selection_key="booking-1") is None


def test_enabled_fee_requires_business_verification():
    with pytest.raises(ValidationError, match="verify the processor"):
        SpaPaymentPolicy.model_validate(
            {"gratuity": {"enabled": True, "card_processing_fee_percent": 4}}
        )

    policy = SpaPaymentPolicy.model_validate(
        {
            "gratuity": {
                "enabled": True,
                "card_processing_fee_percent": 4,
                "fee_verified": True,
            }
        }
    )
    assert policy.gratuity.fee_verified is True

    with pytest.raises(ValidationError, match="less than or equal to 4"):
        SpaPaymentPolicy.model_validate(
            {
                "gratuity": {
                    "enabled": True,
                    "card_processing_fee_percent": 5,
                    "fee_verified": True,
                }
            }
        )


def test_custom_message_cannot_make_tipping_mandatory():
    with pytest.raises(ValidationError, match="must not imply"):
        SpaPaymentPolicy.model_validate(
            {
                "gratuity": {
                    "enabled": True,
                    "card_processing_fee_percent": 0,
                    "custom_message": "You must tip your provider.",
                }
            }
        )

    for message in ("Tipping is required.", "Guests have to leave a tip."):
        with pytest.raises(ValidationError, match="must not imply"):
            SpaPaymentPolicy.model_validate(
                {
                    "gratuity": {
                        "enabled": True,
                        "card_processing_fee_percent": 0,
                        "custom_message": message,
                    }
                }
            )

    with pytest.raises(ValidationError, match="must not imply"):
        SpaPaymentPolicy.model_validate(
            {
                "gratuity": {
                    "enabled": True,
                    "card_processing_fee_percent": 0,
                    "custom_message": "You have to tip your service provider.",
                }
            }
        )


def test_custom_fee_claim_must_be_verified_and_match_configured_percentage():
    with pytest.raises(ValidationError, match="unverified"):
        SpaPaymentPolicy.model_validate(
            {
                "gratuity": {
                    "enabled": True,
                    "card_processing_fee_percent": 0,
                    "custom_message": "Card gratuities have a processing fee.",
                }
            }
        )
    with pytest.raises(ValidationError, match="must match"):
        SpaPaymentPolicy.model_validate(
            {
                "gratuity": {
                    "enabled": True,
                    "card_processing_fee_percent": 4,
                    "fee_verified": True,
                    "custom_message": "Card gratuities have a 5% processing fee.",
                }
            }
        )

    for message in (
        "Card gratuities have a 5 percent processing fee.",
        "Card gratuities have a five percent processing fee.",
    ):
        with pytest.raises(ValidationError, match="must match"):
            SpaPaymentPolicy.model_validate(
                {
                    "gratuity": {
                        "enabled": True,
                        "card_processing_fee_percent": 4,
                        "fee_verified": True,
                        "custom_message": message,
                    }
                }
            )


def test_valid_custom_message_with_matching_written_percentage_is_preserved():
    custom_message = (
        "Card gratuities have a four percent processing fee, and tipping is always optional."
    )
    policy = SpaPaymentPolicy.model_validate(
        {
            "gratuity": {
                "enabled": True,
                "card_processing_fee_percent": 4,
                "fee_verified": True,
                "custom_message": custom_message,
            }
        }
    )

    assert (
        reminder_message(policy.gratuity.model_dump(), selection_key="valid-custom")
        == custom_message
    )


def test_verified_default_script_describes_exact_configured_fee():
    message = reminder_message(verified_policy(), selection_key="booking-1")
    assert message == (
        "Just a little reminder before your visit — gratuities are always appreciated, "
        "and cash is preferred. If you choose to leave a gratuity using a card, there "
        "is a 4% processing fee. We just wanted to let you know ahead of time so there "
        "are no surprises. We look forward to seeing you!"
    )
    assert fee_may_be_spoken(verified_policy())


def test_unverified_runtime_data_never_speaks_a_fee():
    malformed = verified_policy(fee_verified=False)
    message = reminder_message(malformed, selection_key="booking-2")
    assert "processing fee" not in message.lower()
    assert "4%" not in message
    assert not fee_may_be_spoken(malformed)


def test_malformed_string_booleans_fail_closed():
    disabled = verified_policy(enabled="false", fee_verified="true")
    assert reminder_message(disabled, selection_key="malformed-disabled") is None

    unverified = verified_policy(fee_verified="true")
    message = reminder_message(unverified, selection_key="malformed-unverified")
    assert message is not None
    assert "4%" not in message
    assert "processing fee" not in message.lower()


@pytest.mark.parametrize(
    "message",
    [
        "Card tips include a convenience fee.",
        "Card tips include an extra transaction charge.",
    ],
)
def test_custom_fee_synonyms_require_verification(message):
    with pytest.raises(ValidationError, match="unverified"):
        SpaPaymentPolicy.model_validate(
            {
                "gratuity": {
                    "enabled": True,
                    "card_processing_fee_percent": 0,
                    "custom_message": message,
                }
            }
        )


def test_automatic_gratuity_language_is_rejected():
    with pytest.raises(ValidationError, match="must not imply"):
        SpaPaymentPolicy.model_validate(
            {
                "gratuity": {
                    "enabled": True,
                    "card_processing_fee_percent": 0,
                    "custom_message": "A gratuity will be automatically added.",
                }
            }
        )


def test_unsafe_or_mismatched_custom_copy_falls_back_to_verified_copy():
    mandatory = reminder_message(
        verified_policy(custom_message="Tips are mandatory and the fee is 9%."),
        selection_key="booking-3",
    )
    assert "mandatory" not in mandatory.lower()
    assert "9%" not in mandatory
    assert "4%" in mandatory


def test_phrase_selection_rotates_but_is_stable_for_a_booking():
    policy = verified_policy(use_voice_personality=True)
    messages = {
        reminder_message(policy, selection_key=f"booking-{index}")
        for index in range(100)
    }
    assert len(messages) == 4
    assert reminder_message(policy, selection_key="same") == reminder_message(
        policy, selection_key="same"
    )


def test_gratuity_settings_are_scoped_to_each_spa_account():
    first = make_spa(
        payment_policy={"gratuity": verified_policy(card_processing_fee_percent=3)}
    )
    second = make_spa(payment_policy={})
    assert gratuity_policy_of(first)["card_processing_fee_percent"] == 3
    assert gratuity_policy_of(first)["enabled"] is True
    assert gratuity_policy_of(second)["enabled"] is False


@pytest.mark.asyncio
async def test_booking_routing_snapshots_only_the_active_tenant_policy(monkeypatch):
    from app.services import appointment_booking_service as booking_service

    spa = make_spa(
        payment_policy={"gratuity": verified_policy(card_processing_fee_percent=3)}
    )
    session = CallSession(
        call_sid="gratuity-routing",
        direction="inbound",
        from_number="+15550000002",
        to_number=spa.twilio_phone_number,
        tenant_id=str(spa.id),
    )

    async def load_spa(_db, _scope):
        return spa

    monkeypatch.setattr(booking_service, "_load_spa", load_spa)
    await booking_service._prepare(object(), session)
    assert session.entities["gratuity_policy"]["enabled"] is True
    assert session.entities["gratuity_policy"]["card_processing_fee_percent"] == 3


@pytest.mark.asyncio
async def test_spa_fact_answer_reassures_card_is_accepted_and_uses_verified_fee():
    spa = make_spa(payment_policy={"gratuity": verified_policy()})
    result = await lookup_spa_facts(spa, topic="gratuity")
    assert result["status"] == "ok"
    assert "optional" in result["message"].lower()
    assert "card gratuities are accepted" in result["message"].lower()
    assert "4%" in result["message"]


@pytest.mark.asyncio
async def test_disabled_spa_fact_does_not_invent_a_fee():
    spa = make_spa(payment_policy={})
    result = await lookup_spa_facts(spa, topic="gratuity")
    assert result["status"] == "unknown"
    assert "not configured" in result["message"].lower()
    assert "4%" not in result["message"]
    assert result["gratuity"]["card_processing_fee_percent"] is None


@pytest.mark.asyncio
async def test_model_fact_payload_hides_unverified_fee_and_raw_custom_copy():
    spa = make_spa(
        payment_policy={
            "gratuity": {
                "enabled": True,
                "timing": "disabled",
                "cash_preferred": True,
                "card_processing_fee_percent": 4,
                "fee_verified": False,
                "custom_message": "A gratuity will be automatically added.",
            }
        }
    )
    result = await lookup_spa_facts(spa, topic="gratuity")
    assert result["status"] == "ok"
    assert "4%" not in result["message"]
    assert result["gratuity"]["card_processing_fee_percent"] is None
    assert result["gratuity"]["fee_verified"] is False
    assert "custom_message" not in result["gratuity"]
    assert "gratuity" not in result["payment"]
    serialized = json.dumps(result)
    assert "automatically added" not in serialized
    assert "custom_message" not in serialized


def test_authoritative_success_adds_reminder_once_before_closing_question():
    voice = pending_voice()
    voice.session.entities["gratuity_policy"] = verified_policy()
    payload = json.dumps(
        {
            "status": "booked",
            "appointment_id": "local-gratuity",
            "external_booking_id": "square-gratuity",
            "card_status": "not_required",
        }
    )
    spoken = voice._authoritative_tool_followup("confirm_appointment", payload)
    assert "4% processing fee" in spoken
    assert spoken.endswith("Is there anything else I can help you with today?")
    assert voice._authoritative_tool_followup("confirm_appointment", payload) == ""
    assert voice.session.entities["gratuity_reminder_spoken"] is True


def test_outbound_realtime_success_uses_the_same_post_confirmation_reminder():
    voice = pending_voice()
    voice.session.direction = "outbound"
    voice.session.entities["gratuity_policy"] = verified_policy()
    spoken = voice._authoritative_tool_followup(
        "confirm_appointment",
        json.dumps(
            {
                "status": "booked",
                "appointment_id": "local-outbound",
                "external_booking_id": "square-outbound",
                "card_status": "not_required",
            }
        ),
    )
    assert "4% processing fee" in spoken
    assert spoken.endswith("Is there anything else I can help you with today?")


def test_failed_booking_never_triggers_gratuity_reminder():
    voice = pending_voice()
    voice.session.entities["gratuity_policy"] = verified_policy()
    spoken = voice._authoritative_tool_followup(
        "confirm_appointment",
        json.dumps({"status": "conflict", "booked": False}),
    )
    assert "gratu" not in spoken.lower()
    assert not voice.session.entities.get("gratuity_reminder_spoken")
    assert not voice.session.entities.get("pending_gratuity_reminder")


def test_closing_timing_waits_for_the_caller_and_is_consumed_once():
    voice = pending_voice()
    voice.session.entities["gratuity_policy"] = verified_policy(
        timing="during_closing"
    )
    payload = json.dumps(
        {
            "status": "booked",
            "appointment_id": "local-closing",
            "external_booking_id": "square-closing",
            "card_status": "not_required",
        }
    )
    confirmation = voice._authoritative_tool_followup("confirm_appointment", payload)
    assert "processing fee" not in confirmation.lower()
    assert voice.session.entities.get("pending_gratuity_reminder")

    first_close = voice._wrap_up_message()
    second_close = voice._wrap_up_message()
    assert "4% processing fee" in first_close
    assert "processing fee" not in second_close.lower()


def test_prior_gratuity_discussion_suppresses_proactive_repeat():
    voice = pending_voice()
    voice.session.entities["gratuity_policy"] = verified_policy()
    voice.session.entities["gratuity_discussed"] = True
    spoken = voice._authoritative_tool_followup(
        "confirm_appointment",
        json.dumps(
            {
                "status": "booked",
                "appointment_id": "local-discussed",
                "external_booking_id": "square-discussed",
                "card_status": "not_required",
            }
        ),
    )
    assert "processing fee" not in spoken.lower()


def test_question_answer_never_quotes_unverified_fee():
    answer = gratuity_question_answer(verified_policy(fee_verified=False))
    assert "4%" not in answer
    assert "do not state" in answer.lower()


@pytest.mark.asyncio
async def test_disabled_reminder_timing_still_answers_direct_policy_questions():
    policy = verified_policy(timing="disabled")
    assert reminder_message(policy, selection_key="disabled-reminder") is None
    assert "4% processing fee" in gratuity_question_answer(policy)

    spa = make_spa(payment_policy={"gratuity": policy})
    result = await lookup_spa_facts(spa, topic="gratuity")
    assert result["status"] == "ok"
    assert "4% processing fee" in result["message"]


def test_legacy_prompt_gets_only_the_active_tenant_gratuity_facts():
    spa = make_spa(payment_policy={"gratuity": verified_policy()})
    context = build_spa_prompt_context(spa)
    assert "AUTHORITATIVE SPA FACTS — GRATUITIES" in context
    assert "4% processing fee" in context


def test_legacy_voice_path_inserts_after_success_and_never_on_failure():
    from app.api.v1.telephony import _with_gratuity_reminder

    session = CallSession(
        call_sid="legacy-gratuity",
        direction="inbound",
        from_number="+15550000002",
        to_number="+15550000001",
    )
    session.entities["gratuity_policy"] = verified_policy()
    appointment = SimpleNamespace(
        id="local", external_booking_id="provider", card_status="not_required"
    )
    success = SimpleNamespace(outcome=BookingOutcome.BOOKED, appointment=appointment)
    failure = SimpleNamespace(outcome=BookingOutcome.CONFLICT, appointment=None)

    failed_reply = _with_gratuity_reminder(
        session, "That time is no longer available.", failure
    )
    assert "processing fee" not in failed_reply.lower()

    confirmed = _with_gratuity_reminder(
        session,
        "Your appointment is confirmed. Is there anything else I can help you with today?",
        success,
    )
    assert "4% processing fee" in confirmed
    assert confirmed.endswith("Is there anything else I can help you with today?")


def test_legacy_direct_tip_question_suppresses_later_proactive_repeat():
    from app.api.v1.telephony import _note_gratuity_discussion, _with_gratuity_reminder

    session = CallSession(
        call_sid="legacy-discussed",
        direction="inbound",
        from_number="+15550000002",
        to_number="+15550000001",
    )
    session.entities["gratuity_policy"] = verified_policy()
    _note_gratuity_discussion(session, "Can I leave the tip on my card?")

    appointment = SimpleNamespace(
        id="local", external_booking_id="provider", card_status="not_required"
    )
    success = SimpleNamespace(outcome=BookingOutcome.BOOKED, appointment=appointment)
    reply = _with_gratuity_reminder(
        session,
        "Your appointment is confirmed. Is there anything else I can help you with today?",
        success,
    )
    assert session.entities["gratuity_discussed"] is True
    assert "processing fee" not in reply.lower()


def test_pending_card_reservation_does_not_trigger_xai_or_legacy_reminder():
    voice = pending_voice()
    voice.session.entities["gratuity_policy"] = verified_policy()
    payload = json.dumps(
        {
            "status": "booked",
            "appointment_id": "local-pending-card",
            "external_booking_id": "square-pending-card",
            "card_status": "pending_card",
            "card_sms": "sent",
        }
    )
    spoken = voice._authoritative_tool_followup("confirm_appointment", payload)
    assert "pending your card on file" in spoken.lower()
    assert "processing fee" not in spoken.lower()
    assert not voice.session.entities.get("gratuity_reminder_spoken")

    from app.api.v1.telephony import _with_gratuity_reminder

    session = CallSession(
        call_sid="legacy-pending-card",
        direction="inbound",
        from_number="+15550000002",
        to_number="+15550000001",
    )
    session.entities["gratuity_policy"] = verified_policy()
    appointment = SimpleNamespace(
        id="local", external_booking_id="provider", card_status="pending_card"
    )
    success = SimpleNamespace(outcome=BookingOutcome.BOOKED, appointment=appointment)
    legacy = _with_gratuity_reminder(
        session,
        "Your reservation is pending your card on file.",
        success,
    )
    assert "processing fee" not in legacy.lower()
    assert not session.entities.get("gratuity_reminder_spoken")


@pytest.mark.parametrize("card_status", [None, "unknown", "not_supported"])
def test_unresolved_required_card_state_defers_xai_and_legacy_reminder(card_status):
    voice = pending_voice()
    voice.session.entities["gratuity_policy"] = verified_policy()
    voice.session.entities["card_on_file_required"] = True
    payload = {
        "status": "booked",
        "appointment_id": f"local-{card_status or 'missing'}",
        "external_booking_id": f"provider-{card_status or 'missing'}",
    }
    if card_status is not None:
        payload["card_status"] = card_status

    spoken = voice._authoritative_tool_followup(
        "confirm_appointment", json.dumps(payload)
    )
    assert "processing fee" not in spoken.lower()
    assert not voice.session.entities.get("gratuity_reminder_spoken")
    assert not voice.session.entities.get("pending_gratuity_reminder")

    from app.api.v1.telephony import _with_gratuity_reminder

    session = CallSession(
        call_sid=f"legacy-{card_status or 'missing'}",
        direction="inbound",
        from_number="+15550000002",
        to_number="+15550000001",
    )
    session.entities["gratuity_policy"] = verified_policy()
    session.entities["card_on_file_required"] = True
    appointment_data = {"id": "local", "external_booking_id": "provider"}
    if card_status is not None:
        appointment_data["card_status"] = card_status
    success = SimpleNamespace(
        outcome=BookingOutcome.BOOKED,
        appointment=SimpleNamespace(**appointment_data),
    )
    legacy = _with_gratuity_reminder(
        session, "Your appointment is confirmed.", success
    )
    assert "processing fee" not in legacy.lower()
    assert not session.entities.get("gratuity_reminder_spoken")
    assert not session.entities.get("pending_gratuity_reminder")


def test_legacy_closing_timing_waits_for_a_real_signoff():
    from app.api.v1.telephony import _with_gratuity_reminder

    session = CallSession(
        call_sid="legacy-closing",
        direction="inbound",
        from_number="+15550000002",
        to_number="+15550000001",
    )
    session.entities["gratuity_policy"] = verified_policy(timing="during_closing")
    appointment = SimpleNamespace(
        id="local", external_booking_id="provider", card_status="not_required"
    )
    success = SimpleNamespace(outcome=BookingOutcome.BOOKED, appointment=appointment)

    confirmed = _with_gratuity_reminder(
        session,
        "Your appointment is confirmed. Is there anything else I can help you with today?",
        success,
    )
    assert "processing fee" not in confirmed.lower()
    closing = _with_gratuity_reminder(
        session, "Perfect. Have a great day!", None
    )
    assert "4% processing fee" in closing
    assert "processing fee" not in _with_gratuity_reminder(
        session, "Have a great day!", None
    ).lower()
