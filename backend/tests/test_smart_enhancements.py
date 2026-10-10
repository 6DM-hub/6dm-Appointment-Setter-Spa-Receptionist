"""No customer outreach or live provider writes. Exercise state and provider boundaries."""
import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy.dialects import postgresql
from app.models import UserRole
from app.models.enhancement_offer import EnhancementOffer
from app.schemas.enhancements import EnhancementSettings
from app.schemas.spa_account import SpaAccountUpdate
from app.services import enhancements as engine
from app.services.booking_state import get_draft, save_draft, proposal_fingerprint
from app.services.booking_conversation import offer_accepted
from app.services.booking_adapters.base import AvailabilityVerdict
from tests.test_confirmation_pause import pending_voice
from tests.conftest import make_user, make_spa

def setup():
    voice = pending_voice()
    voice.session.tenant_id = uuid.uuid4()
    draft = get_draft(voice.session)
    draft.service_description = "Massage 60"
    draft.end_iso = "2026-10-10T18:30:00+00:00"
    draft.selected_slot.update(service_variation_id="BASE", service_variation_version=1,
                               team_member_id="STAFF", location_id="LOCATION", duration_minutes=60)
    draft.verified_fingerprint = proposal_fingerprint(draft)
    save_draft(voice.session, draft)
    settings = dict(enabled=True, rules=[dict(base_service="Massage 60", target_service="Massage 90", priority=1)])
    spa = make_spa(id=voice.session.tenant_id, enhancement_settings=settings,
        services=[dict(name="Massage 90", duration_minutes=90, square_variation_id="UPGRADE")])
    slot = dict(draft.selected_slot, service_variation_id="UPGRADE", duration_minutes=90)
    adapter = SimpleNamespace(provider="square", check_availability=AsyncMock(return_value=AvailabilityVerdict.ok(slot=slot)))
    async def request(method, path):
        variation = path.split("/")[-1]
        return {"object": {"id": variation, "version": 1, "item_variation_data": {
            "pricing_type": "FIXED_PRICING", "price_money": {"amount": 10000 if variation == "BASE" else 14000, "currency": "USD"},
            "service_duration": (60 if variation == "BASE" else 90) * 60000}}}
    adapter._request = AsyncMock(side_effect=request)
    routing = SimpleNamespace(spa=spa, adapter=adapter)
    result = SimpleNamespace(scalars=lambda: [], scalar_one_or_none=lambda: None)
    db = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())
    return voice, routing, db


def setup_append():
    """A read-only, atomically verified massage followed by a facial."""
    voice, routing, db = setup()
    routing.spa.enhancement_settings = dict(
        enabled=True,
        rules=[dict(
            base_service="Massage 60",
            target_service="European Facial 60",
            priority=1,
            offer_type="append",
            requires_resources=True,
        )],
    )
    routing.spa.services = [dict(
        name="European Facial 60",
        duration_minutes=60,
        square_variation_id="FACIAL",
    )]

    async def request(method, path):
        variation = path.split("/")[-1]
        assert method == "GET" and variation in {"BASE", "FACIAL"}
        return {"object": {
            "id": variation,
            "version": 1 if variation == "BASE" else 2,
            "item_variation_data": {
                "pricing_type": "FIXED_PRICING",
                "price_money": {
                    "amount": 10000 if variation == "BASE" else 12000,
                    "currency": "USD",
                },
                "service_duration": 60 * 60000,
            },
        }}

    routing.adapter._request = AsyncMock(side_effect=request)
    slot = {
        "start": "2026-10-10T17:30:00+00:00",
        "location_id": "LOCATION",
        "duration_minutes": 120,
        "treatment_minutes": 120,
        "team_member_id": "STAFF",
        "service_variation_id": "BASE",
        "service_variation_version": 1,
        "visit_segments": [
            {
                "service_name": "Massage 60",
                "duration_minutes": 60,
                "service_variation_id": "BASE",
                "service_variation_version": 1,
                "team_member_id": "STAFF",
                "provider_name": "Massage Therapist",
                "start": "2026-10-10T17:30:00+00:00",
                "end": "2026-10-10T18:30:00+00:00",
            },
            {
                "service_name": "European Facial 60",
                "duration_minutes": 60,
                "service_variation_id": "FACIAL",
                "service_variation_version": 2,
                "team_member_id": "ESTHETICIAN",
                "provider_name": "Esthetician",
                "start": "2026-10-10T18:30:00+00:00",
                "end": "2026-10-10T19:30:00+00:00",
            },
        ],
    }
    routing.adapter.list_openings = AsyncMock(return_value=[slot])
    return voice, routing, db, slot


def setup_chained_append():
    """An existing two-service visit followed by a provider-verified facial."""
    voice, routing, db, _ = setup_append()
    draft = get_draft(voice.session)
    draft.service_description = "Massage 60 + Extended Scalp 15"
    draft.duration_minutes = 85
    draft.preferred_staff = "Massage Therapist"
    draft.provider_id = "STAFF"
    draft.end_iso = "2026-10-10T18:55:00+00:00"
    draft.selected_slot = {
        "start": "2026-10-10T17:30:00+00:00",
        "location_id": "LOCATION",
        "duration_minutes": 85,
        "treatment_minutes": 75,
        "team_member_id": "STAFF",
        "service_variation_id": "BASE",
        "service_variation_version": 1,
        "visit_segments": [
            {
                "service_name": "Massage 60",
                "duration_minutes": 60,
                "service_variation_id": "BASE",
                "service_variation_version": 1,
                "team_member_id": "STAFF",
                "provider_name": "Massage Therapist",
                "resource_ids": ["MASSAGE_ROOM"],
                "intermission_minutes": 5,
                "start": "2026-10-10T17:30:00+00:00",
                "end": "2026-10-10T18:30:00+00:00",
            },
            {
                "service_name": "Extended Scalp 15",
                "duration_minutes": 15,
                "service_variation_id": "SCALP",
                "service_variation_version": 3,
                "team_member_id": "STAFF",
                "provider_name": "Massage Therapist",
                "resource_ids": ["MASSAGE_ROOM"],
                "intermission_minutes": 5,
                "start": "2026-10-10T18:35:00+00:00",
                "end": "2026-10-10T18:50:00+00:00",
            },
        ],
    }
    draft.verified_fingerprint = proposal_fingerprint(draft)
    save_draft(voice.session, draft)
    routing.spa.enhancement_settings = dict(
        enabled=True,
        rules=[dict(
            base_service="Massage 60 + Extended Scalp 15",
            target_service="European Facial 60",
            priority=1,
            offer_type="append",
            requires_resources=True,
        )],
    )
    routing.spa.services = [
        dict(name="Massage 60", duration_minutes=60, square_variation_id="BASE"),
        dict(name="Extended Scalp 15", duration_minutes=15, square_variation_id="SCALP"),
        dict(
            name="European Facial 60",
            duration_minutes=60,
            cleanup_buffer_minutes=10,
            square_variation_id="FACIAL",
        ),
    ]
    prices = {"BASE": 10000, "SCALP": 3000, "FACIAL": 12000}
    durations = {"BASE": 60, "SCALP": 15, "FACIAL": 60}
    versions = {"BASE": 1, "SCALP": 3, "FACIAL": 2}

    async def request(method, path):
        variation = path.split("/")[-1]
        assert method == "GET" and variation in prices
        return {"object": {
            "id": variation,
            "version": versions[variation],
            "item_variation_data": {
                "pricing_type": "FIXED_PRICING",
                "price_money": {"amount": prices[variation], "currency": "USD"},
                "service_duration": durations[variation] * 60000,
            },
        }}

    routing.adapter._request = AsyncMock(side_effect=request)
    slot = copy.deepcopy(draft.selected_slot)
    slot.update(duration_minutes=155, treatment_minutes=135)
    slot["visit_segments"].append({
        "service_name": "European Facial 60",
        "duration_minutes": 60,
        "service_variation_id": "FACIAL",
        "service_variation_version": 2,
        "team_member_id": "ESTHETICIAN",
        "provider_name": "Esthetician",
        "resource_ids": ["FACIAL_ROOM"],
        "intermission_minutes": 10,
        "start": "2026-10-10T18:55:00+00:00",
        "end": "2026-10-10T19:55:00+00:00",
    })
    routing.adapter.list_openings = AsyncMock(return_value=[slot])
    return voice, routing, db, slot


@pytest.mark.asyncio
async def test_automatic_engine_does_not_repeat_consultation_addon():
    voice, routing, db = setup()
    voice.session.entities["consultation_addon_names"] = ["Massage 90"]

    assert await engine.prepare(db, voice.session, routing) is None
    routing.adapter.check_availability.assert_not_awaited()
    assert voice.session.entities.get(engine.KEY) is None


@pytest.mark.asyncio
async def test_append_prepare_is_read_only_and_requires_exact_atomic_visit():
    voice, routing, db, slot = setup_append()
    original = copy.deepcopy(get_draft(voice.session).to_dict())

    line = await engine.prepare(db, voice.session, routing)

    assert "esthetician is available right after your massage" in line
    assert get_draft(voice.session).to_dict() == original
    routing.adapter.check_availability.assert_not_awaited()
    routing.adapter.list_openings.assert_awaited_once()
    ctx, range_start, range_end = routing.adapter.list_openings.call_args.args
    assert ctx.service_description == "Massage 60 + European Facial 60"
    assert ctx.preferred_staff is None and ctx.provider_id is None
    assert ctx.start.isoformat() == original["selected_slot"]["start"].replace("Z", "+00:00")
    assert (ctx.end - ctx.start).total_seconds() == 120 * 60
    assert range_start == ctx.start and range_end > range_start
    assert voice.session.entities[engine.KEY]["target_slot"] == slot


@pytest.mark.asyncio
async def test_append_allows_provider_verified_transition_gap_and_counts_reserved_time():
    voice, routing, db, slot = setup_append()
    slot["visit_segments"][0]["intermission_minutes"] = 15
    slot["visit_segments"][1].update(
        start="2026-10-10T18:45:00+00:00",
        end="2026-10-10T19:45:00+00:00",
    )
    slot["duration_minutes"] = 135

    line = await engine.prepare(db, voice.session, routing)

    assert "available following your massage" in line
    assert "right after" not in line
    handled, price_line, _, resume = engine.respond(voice.session, "yes")
    assert handled and not resume
    assert "75 additional minutes" in price_line
    assert engine.respond(voice.session, "yes please")[3]
    assert get_draft(voice.session).duration_minutes == 135


@pytest.mark.asyncio
async def test_append_extends_existing_visit_and_prices_every_existing_segment():
    voice, routing, db, slot = setup_chained_append()
    original = copy.deepcopy(get_draft(voice.session).to_dict())

    line = await engine.prepare(db, voice.session, routing)

    assert "European Facial 60" in line
    assert get_draft(voice.session).to_dict() == original
    ctx = routing.adapter.list_openings.call_args.args[0]
    assert ctx.service_description == (
        "Massage 60 + Extended Scalp 15 + European Facial 60"
    )
    assert ctx.preferred_staff is None and ctx.provider_id is None
    assert (ctx.end - ctx.start).total_seconds() == 155 * 60
    handled, price_line, _, resume = engine.respond(voice.session, "yes")
    assert handled and not resume
    assert "120.00 extra" in price_line
    assert "70 additional minutes" in price_line
    assert "250.00 total" in price_line

    assert engine.respond(voice.session, "yes please")[3]
    combined = get_draft(voice.session)
    assert combined.selected_slot == slot
    assert combined.duration_minutes == 155
    assert combined.end_iso == "2026-10-10T20:05:00+00:00"
    assert combined.preferred_staff is None
    assert combined.provider_id is None
    assert combined.square_variation_id == "BASE"
    assert [segment["service_variation_id"] for segment in combined.selected_slot["visit_segments"]] == [
        "BASE",
        "SCALP",
        "FACIAL",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "mutate"),
    [
        ("existing variation", lambda slot: slot["visit_segments"][1].update(service_variation_id="OTHER")),
        ("existing version", lambda slot: slot["visit_segments"][1].update(service_variation_version=9)),
        ("existing provider", lambda slot: slot["visit_segments"][1].update(team_member_id="OTHER")),
        ("existing duration", lambda slot: slot["visit_segments"][1].update(duration_minutes=20)),
        ("existing start", lambda slot: slot["visit_segments"][1].update(start="2026-10-10T18:40:00+00:00")),
        ("existing end", lambda slot: slot["visit_segments"][1].update(end="2026-10-10T18:45:00+00:00")),
        ("existing resources", lambda slot: slot["visit_segments"][1].update(resource_ids=["OTHER_ROOM"])),
        ("existing buffer", lambda slot: slot["visit_segments"][1].update(intermission_minutes=0)),
    ],
)
async def test_append_rejects_any_change_to_existing_visit_segment(change, mutate):
    voice, routing, db, slot = setup_chained_append()
    original = copy.deepcopy(get_draft(voice.session).to_dict())
    mutate(slot)

    assert await engine.prepare(db, voice.session, routing) is None, change
    assert get_draft(voice.session).to_dict() == original
    assert voice.session.entities[engine.KEY]["phase"] == "skipped"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "mutate"),
    [
        ("different root start", lambda slot: slot.update(start="2026-10-10T17:45:00+00:00")),
        ("different location", lambda slot: slot.update(location_id="OTHER")),
        ("different base variation", lambda slot: slot["visit_segments"][0].update(service_variation_id="OTHER")),
        ("different base version", lambda slot: slot["visit_segments"][0].update(service_variation_version=7)),
        ("different massage therapist", lambda slot: slot["visit_segments"][0].update(team_member_id="OTHER")),
        ("different target variation", lambda slot: slot["visit_segments"][1].update(service_variation_id="OTHER")),
        ("different target version", lambda slot: slot["visit_segments"][1].update(service_variation_version=7)),
        ("missing target provider", lambda slot: slot["visit_segments"][1].update(team_member_id=None)),
        ("invalid shifted target start", lambda slot: slot["visit_segments"][1].update(start="2026-10-10T18:45:00+00:00")),
        ("reversed services", lambda slot: slot["visit_segments"].reverse()),
    ],
)
async def test_append_rejects_non_exact_or_non_adjacent_visit(change, mutate):
    voice, routing, db, slot = setup_append()
    original = copy.deepcopy(get_draft(voice.session).to_dict())
    mutate(slot)

    assert await engine.prepare(db, voice.session, routing) is None, change
    assert get_draft(voice.session).to_dict() == original
    assert voice.session.entities[engine.KEY]["phase"] == "skipped"


@pytest.mark.asyncio
async def test_append_decline_preserves_original_visit_without_booking_consent():
    voice, routing, db, _ = setup_append()
    original = copy.deepcopy(get_draft(voice.session).to_dict())
    await engine.prepare(db, voice.session, routing)

    handled, line, event, resume = engine.respond(voice.session, "no thanks")

    assert handled and event == "declined" and not resume
    assert "original" in line
    assert get_draft(voice.session).to_dict() == original
    assert not offer_accepted(voice.session)


@pytest.mark.asyncio
async def test_append_two_step_acceptance_binds_complete_provider_visit():
    voice, routing, db, slot = setup_append()
    original = copy.deepcopy(get_draft(voice.session).to_dict())
    await engine.prepare(db, voice.session, routing)

    handled, line, event, resume = engine.respond(voice.session, "yes")
    assert handled and event is None and not resume
    assert "120.00 extra" in line and "60 additional minutes" in line
    assert "220.00 total" in line and "book that addition" in line
    assert get_draft(voice.session).to_dict() == original
    assert not offer_accepted(voice.session)

    handled, line, event, resume = engine.respond(voice.session, "yes please")
    combined = get_draft(voice.session)
    assert handled and line is None and event == "accepted" and resume
    assert combined.service_description == "Massage 60 + European Facial 60"
    assert combined.duration_minutes == 120
    assert combined.start_iso == "2026-10-10T17:30:00+00:00"
    assert combined.end_iso == "2026-10-10T19:30:00+00:00"
    assert combined.selected_slot == slot
    assert [segment["team_member_id"] for segment in combined.selected_slot["visit_segments"]] == [
        "STAFF",
        "ESTHETICIAN",
    ]
    assert combined.square_variation_id == "BASE"
    assert combined.square_variation_version == 1
    assert combined.provider_verified and offer_accepted(voice.session)
    assert not combined.is_persisted and not combined.confirmation_authorized


@pytest.mark.asyncio
async def test_natural_price_question_keeps_verified_append_offer_pending():
    voice, routing, db, _ = setup_append()
    original = copy.deepcopy(get_draft(voice.session).to_dict())
    await engine.prepare(db, voice.session, routing)

    handled, line, event, resume = engine.respond(voice.session, "How much is it?")

    assert handled and event is None and not resume
    assert "120.00 extra" in line and "60 additional minutes" in line
    assert voice.session.entities[engine.KEY]["phase"] == "price_confirmation"
    assert get_draft(voice.session).to_dict() == original
    assert not offer_accepted(voice.session)

@pytest.mark.asyncio
async def test_verified_offer_preserves_original_and_requires_two_explicit_responses():
    voice, routing, db = setup()
    original = copy.deepcopy(get_draft(voice.session).to_dict())
    line = await engine.prepare(db, voice.session, routing)
    assert "Massage 90" in line and "extra price" in line
    assert get_draft(voice.session).to_dict() == original
    assert routing.adapter.check_availability.await_count == 1
    ctx = routing.adapter.check_availability.call_args.args[0]
    assert ctx.provider_id == "STAFF" and ctx.selected_slot is None
    assert (ctx.end - ctx.start).total_seconds() == 90 * 60
    assert engine.respond(voice.session, "")[1] is None
    assert not offer_accepted(voice.session)
    handled, line, _, resume = engine.respond(voice.session, "yes")
    assert handled and not resume and "40.00 extra" in line and "30 additional" in line and "140.00 total" in line
    assert get_draft(voice.session).to_dict() == original
    assert engine.respond(voice.session, "yes please")[3]
    upgraded = get_draft(voice.session)
    assert upgraded.service_description == "Massage 90" and upgraded.selected_slot["team_member_id"] == "STAFF"
    assert upgraded.booking_id == original["booking_id"] and offer_accepted(voice.session)
    assert not upgraded.is_persisted and not upgraded.confirmation_authorized
    assert await engine.prepare(db, voice.session, routing) is None

@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["offered", "price_confirmation"])
async def test_decline_preserves_base_without_consent_or_repeat(phase):
    voice, routing, db = setup()
    original = get_draft(voice.session).to_dict()
    await engine.prepare(db, voice.session, routing)
    voice.session.entities[engine.KEY]["phase"] = phase
    handled, line, event, resume = engine.respond(voice.session, "no thanks")
    assert handled and event == "declined" and not resume
    assert "original" in line and get_draft(voice.session).to_dict() == original
    assert not offer_accepted(voice.session)
    assert await engine.prepare(db, voice.session, routing) is None

@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unavailable", "staff", "location", "duration", "time", "variation", "version"])
async def test_unavailable_or_conflicting_upgrade_skips_without_mutating_original(change):
    voice, routing, db = setup()
    original = get_draft(voice.session).to_dict()
    slot = dict(routing.adapter.check_availability.return_value.slot)
    changes = {"staff": ("team_member_id", "OTHER"), "location": ("location_id", "OTHER"),
               "duration": ("duration_minutes", 30), "time": ("start", "2026-10-10T19:30:00Z"),
               "variation": ("service_variation_id", "WRONG"), "version": ("service_variation_version", 2)}
    if change == "unavailable":
        verdict = AvailabilityVerdict.no("conflict")
    else:
        key, value = changes[change]
        slot[key] = value
        verdict = AvailabilityVerdict.ok(slot=slot)
    routing.adapter.check_availability.return_value = verdict
    assert await engine.prepare(db, voice.session, routing) is None
    assert get_draft(voice.session).to_dict() == original
    assert not engine.pending(voice.session)

@pytest.mark.asyncio
async def test_missing_catalog_price_skips_before_availability():
    voice, routing, db = setup()
    routing.adapter._request = AsyncMock(return_value={"object": {"id": "UPGRADE", "item_variation_data": {}}})
    assert await engine.prepare(db, voice.session, routing) is None
    routing.adapter.check_availability.assert_not_awaited()

def test_personalization_and_declines_use_only_supported_facts():
    config = EnhancementSettings(enabled=True, rules=[dict(base_service="Base", target_service=t) for t in ["A", "B"]])
    assert engine.ranked_rules(config, "Base", ["B", "B"])[0].target_service == "B"
    assert engine.ranked_rules(config, "Base", ["B"], [SimpleNamespace(status="declined", target_service="B")])[0].target_service == "A"
    config.personalize = False
    assert engine.ranked_rules(config, "Base", ["B"])[0].target_service == "A"
    config.excluded_services = ["Base"]
    assert engine.ranked_rules(config, "Base") == []

@pytest.mark.asyncio
async def test_resource_dependent_offer_is_blocked_without_provider_queries():
    voice, routing, db = setup()
    routing.spa.enhancement_settings["rules"][0]["requires_resources"] = True
    assert await engine.prepare(db, voice.session, routing) is None
    routing.adapter._request.assert_not_awaited()

@pytest.mark.asyncio
async def test_changed_request_invalidates_upgrade_consent():
    voice, routing, db = setup()
    await engine.prepare(db, voice.session, routing)
    draft = get_draft(voice.session)
    draft.guest_name = "Another person"
    save_draft(voice.session, draft)
    assert not engine.respond(voice.session, "yes")[0]
    assert not offer_accepted(voice.session)

@pytest.mark.asyncio
async def test_pending_offer_blocks_model_booking_and_availability_tools():
    voice, routing, db = setup()
    await engine.prepare(db, voice.session, routing)
    assert "awaiting_enhancement_response" in await voice._run_confirm_appointment("{}")
    voice._send_function_output = AsyncMock()
    await voice._handle_function_call({"name": "confirm_appointment", "call_id": "offer-pending", "arguments": "{}"})
    assert "awaiting_enhancement_response" in voice._send_function_output.call_args.args[1]
    assert not get_draft(voice.session).is_persisted

@pytest.mark.asyncio
async def test_voice_waits_for_cost_approval_before_existing_card_flow(monkeypatch):
    voice, routing, db = setup()
    await engine.prepare(db, voice.session, routing)
    voice._record_enhancement = AsyncMock()
    assert await voice._confirm_pending_booking_from_caller("yes")
    assert "40.00 extra" in str(voice.spoken)
    assert "card on file" not in str(voice.spoken)
    voice.spoken.clear()
    assert await voice._confirm_pending_booking_from_caller("yes")
    assert "card on file" in str(voice.spoken)
    assert get_draft(voice.session).confirmation_authorized
    assert get_draft(voice.session).square_variation_id == "UPGRADE"

@pytest.mark.asyncio
async def test_success_reporting_requires_provider_success_and_is_idempotent(monkeypatch):
    from app.services import xai_realtime as xai
    from app.services.appointment_booking_service import BookingOutcome
    voice, routing, db = setup()
    await engine.prepare(db, voice.session, routing)
    engine.respond(voice.session, "yes")
    engine.respond(voice.session, "yes")
    voice.session.entities["card_on_file_required"] = False
    voice.session.add_turn("user", "yes")
    from app.services.booking_conversation import authorize_accepted_offer
    authorize_accepted_offer(voice.session)
    appt = SimpleNamespace(id=uuid.uuid4(), external_booking_id="SQUARE-BOOKING", card_status=None)
    outcome = SimpleNamespace(outcome=BookingOutcome.BOOKED, appointment=appt, card_sms=None, message="Success", to_system_message=lambda: "Provider success")
    class DB:
        async def __aenter__(self): return db
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(xai, "AsyncSessionLocal", DB)
    monkeypatch.setattr(xai, "confirm_booking", AsyncMock(return_value=outcome))
    monkeypatch.setattr(xai, "apply_booking_result", lambda *args: None)
    voice._record_enhancement = AsyncMock()
    import json
    assert json.loads(await voice._run_confirm_appointment("{}"))["booked"]
    voice._record_enhancement.assert_awaited_once_with("booked", external_booking_id="SQUARE-BOOKING")
    await voice._run_confirm_appointment("{}")
    assert voice._record_enhancement.await_count == 1

@pytest.mark.asyncio
async def test_definite_upgrade_conflict_restores_base_without_auto_write(monkeypatch):
    from app.services import xai_realtime as xai
    from app.services.appointment_booking_service import BookingOutcome
    from app.services.booking_conversation import authorize_accepted_offer
    voice, routing, db = setup()
    await engine.prepare(db, voice.session, routing)
    engine.respond(voice.session, "yes"); engine.respond(voice.session, "yes")
    authorize_accepted_offer(voice.session)
    voice.session.entities["card_on_file_required"] = False
    voice.session.add_turn("user", "yes")
    class DB:
        async def __aenter__(self): return db
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(xai, "AsyncSessionLocal", DB)
    confirm = AsyncMock(return_value=SimpleNamespace(outcome=BookingOutcome.CONFLICT, appointment=None))
    monkeypatch.setattr(xai, "confirm_booking", confirm)
    voice._record_enhancement = AsyncMock()
    assert "upgrade_unavailable" in await voice._run_confirm_appointment("{}")
    assert get_draft(voice.session).service_description == "Massage 60"
    assert not get_draft(voice.session).confirmation_authorized
    assert not offer_accepted(voice.session)
    assert confirm.await_count == 1

def test_metrics_do_not_count_acceptance_as_money_or_payment_revenue():
    def row(status, external=None):
        return SimpleNamespace(status=status, external_booking_id=external, incremental_minor=4000,
            currency="USD", base_service="Base", target_service="Upgrade", facts={"accepted": True, "presented": True})
    report = engine.metrics([row("accepted"), row("failed"), row("booked", "provider-id")])
    assert report["accepted"] == 3 and report["booked"] == 1
    assert report["incremental_booking_value_minor"] == {"USD": 4000}
    assert report["realized_revenue"] is None

def test_privacy_tenant_key_and_schema_limits():
    assert engine.customer_key("tenant-a", "+15550001") != engine.customer_key("tenant-b", "+15550001")
    with pytest.raises(ValueError): EnhancementSettings(max_suggestions=2)
    with pytest.raises(ValueError): EnhancementSettings(retention_days=0)
    with pytest.raises(ValueError): SpaAccountUpdate(enhancement_settings=None)
    model = SpaAccountUpdate(services=[dict(name="Massage", square_variation_id="ABC", square_variation_version=4)])
    assert model.model_dump()["services"][0]["square_variation_id"] == "ABC"

@pytest.mark.asyncio
async def test_history_write_has_tenant_and_durable_unique_intent():
    voice, routing, db = setup()
    await engine.prepare(db, voice.session, routing)
    await engine.record(db, voice.session, "presented")
    stmt = db.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "ON CONFLICT ON CONSTRAINT uq_enhancement_intent" in str(stmt)
    assert stmt.params["tenant_id"] == voice.session.tenant_id
    assert stmt.params["intent_key"].startswith(voice.session.call_sid)
    assert voice.session.customer_phone not in str(stmt.params)

def test_foreign_tenant_report_and_history_are_denied(client, principal):
    principal.user = make_user(UserRole.SPA_ADMIN, tenant_id=uuid.uuid4())
    other = uuid.uuid4()
    assert client.get(f"/api/v1/spa-accounts/{other}/enhancements/report").status_code == 404
    assert client.delete(f"/api/v1/spa-accounts/{other}/enhancements/history").status_code == 404

def test_staff_cannot_delete_history(client, principal):
    principal.user = make_user(UserRole.SPA_STAFF, tenant_id=uuid.uuid4())
    assert client.delete(f"/api/v1/spa-accounts/{principal.user.tenant_id}/enhancements/history").status_code == 403

@pytest.mark.asyncio
async def test_mismatched_routing_tenant_never_reads_or_recommends():
    voice, routing, db = setup()
    routing.spa.id = uuid.uuid4()
    assert await engine.prepare(db, voice.session, routing) is None
    db.execute.assert_not_awaited()

@pytest.mark.asyncio
async def test_customer_opt_out_disables_offers_and_history(monkeypatch):
    voice, routing, db = setup()
    no_match = SimpleNamespace(scalar_one_or_none=lambda: None)
    opted_out = SimpleNamespace(scalar_one_or_none=lambda: {"enhancement_opt_out": True})
    db.execute.side_effect = [no_match, opted_out]
    assert await engine.prepare(db, voice.session, routing) is None
    routing.adapter._request.assert_not_awaited()
    assert engine.KEY not in voice.session.entities

@pytest.mark.asyncio
async def test_existing_offer_survives_session_state_loss():
    voice, routing, db = setup()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: uuid.uuid4())
    assert await engine.prepare(db, voice.session, routing) is None
    routing.adapter._request.assert_not_awaited()

@pytest.mark.asyncio
async def test_periodic_retention_deletes_each_tenant_with_its_policy():
    from app.services.enhancement_retention import purge_expired
    from datetime import datetime, timezone
    a, b = uuid.uuid4(), uuid.uuid4()
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(a, {"retention_days": 30}), (b, {"retention_days": 90})])), commit=AsyncMock())
    await purge_expired(db, datetime(2026, 10, 10, tzinfo=timezone.utc))
    deletes = [call.args[0].compile(dialect=postgresql.dialect()) for call in db.execute.call_args_list[1:]]
    assert deletes[0].params["tenant_id_1"] == a and deletes[1].params["tenant_id_1"] == b
    assert deletes[0].params["created_at_1"] != deletes[1].params["created_at_1"]

@pytest.mark.asyncio
async def test_grok_generates_cached_invitations_without_customer_information(monkeypatch):
    from app.services.enhancement_phrasing import generate_phrasing, grok_service
    generate = AsyncMock(return_value='{"phrases":["Would you like to consider {service}?","How does {service} sound as an optional upgrade?"]}')
    monkeypatch.setattr(grok_service, "_chat", generate)
    values = await generate_phrasing()
    assert len(values) == 2
    assert generate.call_args.kwargs["max_tokens"] == 240
    assert "+15550001" not in str(generate.call_args)

@pytest.mark.parametrize("phrase", ["This is popular {service}?", "Would you like {service} for $10?", "Would you like {service} because it treats pain?", "I should not offer other times for {service}?", "Would you like {service} tomorrow?", "Would you like to book {service}?"])
def test_model_phrasing_cannot_claim_facts_or_speak_internal_instructions(phrase):
    from app.schemas.enhancements import EnhancementRule
    with pytest.raises(ValueError):
        EnhancementRule(base_service="Base", target_service="Upgrade", phrase_variants=[phrase])

@pytest.mark.asyncio
async def test_returning_caller_does_not_hear_last_generated_invitation():
    import hashlib
    voice, routing, db = setup()
    one, two = "Would you like to consider {service}?", "How does {service} sound as an optional upgrade?"
    routing.spa.enhancement_settings["rules"][0]["phrase_variants"] = [one, two]
    routing.spa.enhancement_settings["personalize"] = False
    previous = SimpleNamespace(intent_key="OLD:booking", status="presented", target_service="Massage 90", facts={"phrase_id": hashlib.sha256(one.encode()).hexdigest()})
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None, scalars=lambda: [previous])
    line = await engine.prepare(db, voice.session, routing)
    assert line == two.replace("{service}", "Massage 90")

@pytest.mark.asyncio
async def test_optional_lookup_error_is_fail_open_for_original_booking(monkeypatch):
    from app.services import xai_realtime as xai
    voice, routing, db = setup()
    voice.session.entities["smart_enhancements_enabled"] = True
    original = get_draft(voice.session).to_dict()
    class DB:
        async def __aenter__(self): return db
        async def __aexit__(self, *args): pass
    async def fail(*args):
        voice.session.entities[engine.KEY] = {"phase": "checking"}
        raise TimeoutError()
    monkeypatch.setattr(xai, "AsyncSessionLocal", DB)
    monkeypatch.setattr(xai, "_prepare", AsyncMock(return_value=routing))
    monkeypatch.setattr(engine, "prepare", fail)
    assert await voice._maybe_enhancement() is None
    assert get_draft(voice.session).to_dict() == original
    assert not engine.pending(voice.session)

@pytest.mark.asyncio
async def test_decline_with_explicit_original_booking_request_does_not_ask_again():
    voice, routing, db = setup()
    await engine.prepare(db, voice.session, routing)
    handled, line, event, resume = engine.respond(voice.session, "Just book the original")
    assert handled and resume and line is None and event == "declined"
    assert offer_accepted(voice.session)
    assert get_draft(voice.session).service_description == "Massage 60"

@pytest.mark.asyncio
async def test_natural_upgrade_selection_still_requires_cost_approval():
    voice, routing, db = setup()
    await engine.prepare(db, voice.session, routing)
    handled, line, _, resume = engine.respond(voice.session, "I'll take Massage 90")
    assert handled and not resume and "40.00 extra" in line
    assert not offer_accepted(voice.session)
    assert engine.respond(voice.session, "Yes, upgrade it")[3]
