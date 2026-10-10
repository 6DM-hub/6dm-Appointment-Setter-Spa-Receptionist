"""Source-of-truth regressions for the AI spa receptionist.

These tests pin backend enforcement: the LLM is never treated as an
authoritative source of spa facts, availability, payment, or booking success.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.booking_adapters.base import AvailabilityVerdict, BookingContext
from app.services.booking_adapters.providers.square import SquareAdapter
from app.services.booking_state import get_draft, stage
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent, build_realtime_instructions
from app.services.spa_facts import UNKNOWN, lookup_spa_facts
from app.services.xai_realtime import LOOKUP_SPA_FACTS_TOOL, VOICE_TOOLS
from app.schemas.spa_account import SpaAccountCreate
from tests.conftest import make_spa
from tests.test_confirmation_pause import pending_voice


def _session() -> CallSession:
    return CallSession(
        "call-sot",
        "inbound",
        "+15550000002",
        "+15550000001",
        tenant_id="tenant-1",
    )


def test_lookup_spa_facts_is_a_realtime_tool():
    assert LOOKUP_SPA_FACTS_TOOL in VOICE_TOOLS
    assert LOOKUP_SPA_FACTS_TOOL["name"] == "lookup_spa_facts"


def test_realtime_prompt_requires_fact_lookup_not_memory():
    text = build_realtime_instructions("Harbor Spa", "Persona only.")
    assert "lookup_spa_facts" in text
    assert "Never invent" in text


def test_realtime_prompt_contains_grounded_consultation_flow():
    text = build_realtime_instructions("Harbor Spa", "Persona only.")
    assert "FACIAL AND MASSAGE CONSULTATION RULES" in text
    assert "ask one question at a time" in text
    assert "For every facial booking request" in text
    assert "For every massage booking request" in text
    assert "backend owns the full consultation" in text
    assert "never restart consultation through lookup_spa_facts" in text
    assert "Offer both only when the backend confirms both" in text
    assert "Do not independently look up or repeat an add-on offer" in text
    assert "qualified providers and any provider-managed transition time" in text
    assert text.index("Persona only.") < text.index("FACIAL AND MASSAGE CONSULTATION RULES")


def test_realtime_fact_tool_does_not_duplicate_server_managed_consultation():
    properties = LOOKUP_SPA_FACTS_TOOL["parameters"]["properties"]
    topics = properties["topic"]["enum"]
    assert "consultation" not in topics
    assert "managed by propose_appointment" in LOOKUP_SPA_FACTS_TOOL["description"]
    assert "consultation_kind" not in properties


def test_service_consultation_metadata_survives_account_validation():
    account = SpaAccountCreate.model_validate({
        "name": "Harbor Spa",
        "services": [{
            "name": "House Hydrating Facial",
            "duration_minutes": 60,
            "square_variation_id": "  square-hydrating-60  ",
            "category": "  Facial  ",
            "consultation_kind": "facial",
            "consultation_category": "  hydrating  ",
            "consultation_tags": [" dry ", "dull"],
            "aliases": ["hydration facial"],
            "service_family": "  House Hydrating Facial  ",
            "approved_benefit": "  Supports a refreshed feel.  ",
        }],
        "enhancement_settings": {"excluded_services": ["  Deep Tissue Massage  "]},
    })
    service = account.model_dump()["services"][0]
    assert service["consultation_category"] == "hydrating"
    assert service["consultation_kind"] == "facial"
    assert service["consultation_tags"] == ["dry", "dull"]
    assert service["service_family"] == "House Hydrating Facial"
    assert service["approved_benefit"] == "Supports a refreshed feel."
    assert service["square_variation_id"] == "square-hydrating-60"
    assert account.enhancement_settings.excluded_services == ["Deep Tissue Massage"]


@pytest.mark.asyncio
async def test_consultation_lookup_returns_only_configured_duration_and_addons():
    spa = make_spa(
        services=[
            {
                "name": "Therapeutic Massage",
                "duration_minutes": 60,
                "consultation_kind": "massage",
                "consultation_category": "focused_therapeutic",
            },
            {"name": "Hot Stones", "duration_minutes": 15, "is_add_on": True},
            {"name": "Aromatherapy", "duration_minutes": 5, "is_add_on": True},
            {"name": "60 minute European Facial", "duration_minutes": 60},
        ],
        upsell_rules=[{
            "base_service": "Therapeutic Massage",
            "allowed_upsells": ["Hot Stones", "Made Up Add-on"],
        }],
    )
    result = await lookup_spa_facts(
        spa,
        topic="consultation",
        consultation_kind="massage",
        massage_reason="specific tension",
        massage_areas="shoulders",
        pressure_preference="medium",
        selected_service_name="Therapeutic Massage",
        selected_duration_minutes=60,
    )
    consultation = result["consultation"]
    assert consultation["service"]["name"] == "Therapeutic Massage"
    assert [choice["minutes"] for choice in consultation["durations"]["choices"]] == [60]
    assert consultation["durations"]["missing_minutes"] == [30]
    assert [item["service"]["name"] for item in consultation["addons"]] == ["Hot Stones"]
    assert consultation["sequential_availability_verified"] is False
    assert "post_massage_facial_candidates" not in consultation
    assert result["message"] == (
        "Based on what you shared, Therapeutic Massage is the closest match on our menu."
    )
    assert "configured" not in result["message"].casefold()
    assert "offer" not in result["message"].casefold()
    assert "invent" not in result["message"].casefold()


@pytest.mark.asyncio
async def test_consultation_messages_are_customer_safe_when_kind_or_match_is_missing():
    spa = make_spa(services=[{"name": "European Facial", "duration_minutes": 60}])

    missing_kind = await lookup_spa_facts(spa, topic="consultation")
    unmatched = await lookup_spa_facts(
        spa,
        topic="consultation",
        consultation_kind="facial",
        main_concern="breakouts",
        skin_feel="oily",
    )

    assert missing_kind["message"] == "Would you like help choosing a facial or a massage?"
    assert unmatched["message"] == (
        "I couldn't find an exact match for those preferences on our menu. "
        "Would you like to hear the available facial options?"
    )
    for result in (missing_kind, unmatched):
        message = result["message"].casefold()
        assert "ask the caller" not in message
        assert "configured" not in message
        assert "do not" not in message
        assert "invent" not in message


@pytest.mark.asyncio
async def test_realtime_consultation_persists_only_safe_progress_and_reuses_category(monkeypatch):
    from app.services import xai_realtime

    spa = make_spa(
        services=[
            {
                "name": "60 Minute Therapeutic Massage",
                "service_family": "Therapeutic Massage",
                "consultation_kind": "massage",
                "consultation_category": "focused_therapeutic",
                "duration_minutes": 60,
            },
            {"name": "Hot Stones", "duration_minutes": 15, "is_add_on": True},
        ],
        upsell_rules=[{
            "base_service": "60 Minute Therapeutic Massage",
            "allowed_upsells": ["Hot Stones"],
        }],
    )

    class DBContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(xai_realtime, "AsyncSessionLocal", lambda: DBContext())
    monkeypatch.setattr(
        xai_realtime,
        "_prepare",
        AsyncMock(return_value=SimpleNamespace(spa=spa, adapter=None)),
    )
    voice = pending_voice()
    voice._persist_session = AsyncMock()

    await voice._run_lookup_spa_facts(json.dumps({
        "topic": "consultation",
        "consultation_kind": "massage",
        "massage_reason": "specific tension",
        "massage_areas": "private shoulder detail",
        "pressure_preference": "medium",
        "safety_answered": True,
    }))
    follow_up = json.loads(await voice._run_lookup_spa_facts(json.dumps({
        "topic": "consultation",
        "consultation_kind": "massage",
        "selected_service_name": "60 Minute Therapeutic Massage",
        "selected_duration_minutes": 60,
    })))

    state = voice.session.entities["consultation_state"]
    assert state["category"] == "focused_therapeutic"
    assert state["answered_fields"] == [
        "massage_areas", "massage_reason", "pressure_preference", "safety_answered"
    ]
    assert "private shoulder detail" not in json.dumps(state)
    assert follow_up["consultation"]["remaining_questions"] == []
    assert follow_up["consultation"]["selected_service"]["duration_minutes"] == 60
    assert [
        item["service"]["name"] for item in follow_up["consultation"]["addons"]
    ] == ["Hot Stones"]
    assert voice._persist_session.await_count == 2


@pytest.mark.asyncio
async def test_1_address_comes_from_dashboard():
    spa = make_spa(location="4100 McKinney Ave, Dallas, TX")
    result = await lookup_spa_facts(spa, topic="location")
    assert result["location"]["address"] == "4100 McKinney Ave, Dallas, TX"
    assert result["location"]["source"] == "dashboard"
    assert "Dallas" in result["message"]
    assert "123 Main" not in result["message"]


@pytest.mark.asyncio
async def test_2_missing_address_is_unknown():
    spa = make_spa(location=None)
    result = await lookup_spa_facts(spa, topic="location")
    assert result["status"] == "unknown"
    assert result["message"] == UNKNOWN
    assert result["location"]["verified"] is False


@pytest.mark.asyncio
async def test_3_square_location_wins_operational_address():
    spa = make_spa(location="Stale dashboard street")

    class _Adapter:
        provider = "square"

        async def describe_location(self):
            return {
                "id": "loc_live",
                "name": "Harbor Dallas",
                "timezone": "America/Chicago",
                "status": "ACTIVE",
                "address": {
                    "address_line_1": "500 Live Square Blvd",
                    "locality": "Dallas",
                    "administrative_district_level_1": "TX",
                    "postal_code": "75201",
                },
            }

    result = await lookup_spa_facts(spa, adapter=_Adapter(), topic="location")
    assert result["location"]["source"] == "booking_provider"
    assert result["location"]["location_id"] == "loc_live"
    assert "500 Live Square Blvd" in result["message"]
    assert "Stale dashboard" not in result["message"]


@pytest.mark.asyncio
async def test_4_nonexistent_service_is_not_fabricated():
    spa = make_spa(services=[{"name": "Swedish massage", "duration_minutes": 60}])
    result = await lookup_spa_facts(spa, topic="prices", service_name="unicorn wrap")
    assert result["status"] == "unknown"
    assert result["message"] == UNKNOWN


@pytest.mark.asyncio
async def test_5_exact_unavailable_uses_same_search_alternatives_not_new_probes():
    adapter = SquareAdapter("Harbor", {"access_token": "t", "location_id": "loc_123"})
    start = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
    ctx = BookingContext(
        start=start,
        end=start + timedelta(minutes=60),
        title="Facial",
        customer_phone="+1555",
        service_description="Facial",
    )
    later = start + timedelta(minutes=90)
    square_rows = [
        {
            "start_at": later.isoformat().replace("+00:00", "Z"),
            "location_id": "loc_123",
            "appointment_segments": [
                {
                    "team_member_id": "tm_1",
                    "service_variation_id": "sv_1",
                    "service_variation_version": 1,
                    "duration_minutes": 60,
                }
            ],
        }
    ]

    async def _search(_ctx, _a, _b):
        return square_rows, "sv_1", []

    adapter._search_square_availabilities = _search  # type: ignore[method-assign]
    adapter._validate_config = lambda: None  # type: ignore[method-assign]
    adapter._location = AsyncMock(return_value={"id": "loc_123", "status": "ACTIVE"})
    adapter._resolve_service_variation = AsyncMock(return_value={"id": "sv_1"})

    verdict = await adapter.check_availability(ctx)
    assert verdict.available is False
    assert len(verdict.alternatives) == 1
    assert verdict.alternatives[0]["start"].startswith("2026-10-05")


@pytest.mark.asyncio
async def test_6_earliest_uses_one_list_openings_call():
    from app.services.appointment_booking_service import _stage_earliest

    session = _session()
    session.add_turn("user", "What's your earliest appointment?")
    draft = get_draft(session)
    draft.service_description = "Facial"
    adapter = SimpleNamespace(
        default_title="Spa",
        default_duration_minutes=60,
        provider="square",
        booking_timezone_name=AsyncMock(return_value=None),
        list_openings=AsyncMock(
            return_value=[
                {
                    "start": "2026-10-06T14:00:00Z",
                    "location_id": "loc_123",
                    "team_member_id": "tm",
                    "service_variation_id": "sv",
                    "duration_minutes": 60,
                }
            ]
        ),
        check_availability=AsyncMock(),
    )
    routing = SimpleNamespace(
        adapter=adapter,
        scope=SimpleNamespace(),
        spa=make_spa(timezone="UTC"),
        capacity=1,
        is_outbound_sales=False,
        product="spa",
    )
    intent = AppointmentIntent(intent="schedule", earliest=True, confidence=1.0, service_description="Facial")
    result = await _stage_earliest(None, session, intent, draft, routing)
    adapter.list_openings.assert_awaited_once()
    adapter.check_availability.assert_not_called()
    assert "2026-10-06T14:00:00" in result.message


def test_7_changing_time_invalidates_selected_slot():
    session = _session()
    draft = stage(
        session,
        AppointmentIntent(
            intent="schedule",
            confidence=1.0,
            requested_start_iso="2026-10-05T15:00:00",
            service_description="Facial",
        ),
    )
    draft.selected_slot = {"start": "2026-10-05T15:00:00Z", "location_id": "loc_123"}
    session.entities["booking_draft"] = draft.to_dict()
    updated = stage(
        session,
        AppointmentIntent(
            intent="schedule",
            confidence=1.0,
            requested_start_iso="2026-10-05T16:00:00",
            service_description="Facial",
        ),
    )
    assert updated.selected_slot is None
    assert updated.start_iso == "2026-10-05T16:00:00"


def test_8_changing_service_invalidates_slot():
    session = _session()
    draft = stage(
        session,
        AppointmentIntent(
            intent="schedule",
            confidence=1.0,
            requested_start_iso="2026-10-05T15:00:00",
            service_description="Facial",
        ),
    )
    draft.selected_slot = {"start": "x"}
    session.entities["booking_draft"] = draft.to_dict()
    updated = stage(
        session,
        AppointmentIntent(
            intent="schedule",
            confidence=1.0,
            service_description="Massage",
        ),
    )
    assert updated.selected_slot is None
    assert updated.service_description == "Massage"


def test_9_staff_preference_is_not_silently_dropped():
    session = _session()
    session.add_turn("user", "I'd like to book with Sarah")
    draft = stage(
        session,
        AppointmentIntent(intent="schedule", confidence=1.0, preferred_staff="Sarah"),
    )
    assert draft.preferred_staff == "Sarah"


@pytest.mark.asyncio
async def test_10_and_11_confirm_requires_provider_id():
    from app.services.xai_realtime import XAIVoiceSession

    voice = XAIVoiceSession("call-sot", _session())
    success = voice._authoritative_tool_followup(
        "confirm_appointment",
        '{"status":"booked","appointment_id":"a1","external_booking_id":"sq_1"}',
    )
    assert success is not None
    failure = voice._authoritative_tool_followup(
        "confirm_appointment",
        '{"status":"booked","appointment_id":"a1","external_booking_id":null}',
    )
    assert failure is None
    error = voice._authoritative_tool_followup(
        "confirm_appointment",
        '{"status":"error","message":"Square failed"}',
    )
    assert error is None


def test_12_reschedule_mode_does_not_clear_to_create():
    session = _session()
    draft = stage(session, AppointmentIntent(intent="reschedule", confidence=1.0))
    assert draft.operation_mode == "reschedule"


@pytest.mark.asyncio
async def test_14_unconfigured_upsell_is_unknown():
    spa = make_spa(upsell_rules=[])
    result = await lookup_spa_facts(spa, topic="upsell", service_name="HydroLux5 Facial - Face Only")
    assert result["upsell_configured"] is False
    assert "Do not invent" in result["message"]


@pytest.mark.asyncio
async def test_15_missing_price_is_unknown():
    spa = make_spa(services=[{"name": "Facial"}])
    result = await lookup_spa_facts(spa, topic="prices", service_name="Facial")
    assert result["status"] == "unknown"


@pytest.mark.asyncio
async def test_16_payment_never_collects_raw_cards():
    spa = make_spa(payment_policy={"card_required": True, "collection_mode": "none"})
    result = await lookup_spa_facts(spa, topic="payment")
    assert result["payment"]["collect_raw_card_on_call"] is False
    assert "do not collect" in result["message"].lower() or "do not ask" in result["message"].lower()


@pytest.mark.asyncio
async def test_configured_upsell_is_returned():
    spa = make_spa(
        upsell_rules=[
            {
                "base_service": "HydroLux5 Facial - Face Only",
                "allowed_upsells": ["Neck upgrade", "Decollete upgrade"],
            }
        ]
    )
    result = await lookup_spa_facts(spa, topic="upsell", service_name="HydroLux5 Facial - Face Only")
    assert result["upsell_configured"] is True
    assert "Neck upgrade" in result["message"]


def test_17_same_name_caller_and_staff_stay_separate():
    session = _session()
    session.add_turn("assistant", "How can I help?")
    session.add_turn("user", "My name is Sarah and I'd like to book with Sarah.")
    draft = stage(
        session,
        AppointmentIntent(
            intent="schedule",
            confidence=1.0,
            caller_name="Sarah",
            preferred_staff="Sarah",
        ),
    )
    assert draft.caller_name == "Sarah"
    assert draft.preferred_staff == "Sarah"


def test_guest_name_is_separate_from_caller_and_staff():
    session = _session()
    draft = stage(
        session,
        AppointmentIntent(
            intent="schedule",
            confidence=1.0,
            caller_name="Sarah",
            guest_name="Jessica",
            preferred_staff="Riley",
        ),
    )
    assert draft.caller_name == "Sarah"
    assert draft.guest_name == "Jessica"
    assert draft.preferred_staff == "Riley"


def test_service_clarification_copy_never_speaks_internal_instructions():
    from app.services.appointment_booking_service import _clarification

    verdict = SimpleNamespace(
        reason=(
            "needs_clarification: service_ambiguous: Swedish Massage"
            " | options: Swedish Massage (30 min); Swedish Massage (60 min)"
        )
    )
    spoken = _clarification(verdict)
    assert spoken == (
        "'Swedish Massage' matches more than one service. The options are: "
        "Swedish Massage (30 min); Swedish Massage (60 min). Which one would you like?"
    )
    assert "Do NOT" not in spoken
    assert "Ask the caller" not in spoken
    assert "Do not invent" not in spoken
