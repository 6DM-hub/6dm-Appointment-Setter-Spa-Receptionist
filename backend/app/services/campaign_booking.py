"""Exercise the existing appointment pipeline with a strictly local test adapter.

Never selects a production adapter or sender. Disabled in production; only
marked test contacts qualify. Simulator success is explicitly not Square success.
"""
from datetime import timedelta
from copy import deepcopy
from types import SimpleNamespace
import uuid
from fastapi import HTTPException
from sqlalchemy import select
from app.core.config import settings
from app.models import Appointment, Contact, SpaAccount
from app.services.booking_adapters.base import AvailabilityVerdict, BookingAdapter, BookingProviderError, ExternalBooking
from app.services.booking_adapters.spa_router import SpaBookingAdapter
from app.services.booking_state import get_draft, intent_key, mark_read_back, proposal_fingerprint, record_pure_confirmation
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent


class _TestProvider(BookingAdapter):
    provider = "test_pipeline"
    calendar_label = "the local campaign test calendar"

    async def check_availability(self, ctx):
        minutes = int((ctx.end-ctx.start).total_seconds()/60)
        slot = {"start": ctx.start.isoformat(), "duration_minutes": minutes,
                "service_variation_id": "test:" + ctx.title + ":" + str(minutes),
                "service_variation_version": 1, "location_id": "test-local",
                "team_member_id": ctx.preferred_staff or (ctx.allowed_staff[0] if ctx.allowed_staff else "test-any-provider")}
        if ctx.selected_slot and any(ctx.selected_slot.get(k) != slot[k] for k in slot):
            return AvailabilityVerdict.no("The selected test slot changed. Nothing was booked.")
        return AvailabilityVerdict.ok(slot)

    async def create_booking(self, ctx):
        verdict = await self.check_availability(ctx)
        if not verdict.available or not ctx.booking_reference:
            raise BookingProviderError("Test provider did not accept the exact booking.")
        return ExternalBooking(provider=self.provider, external_id="test-pipeline:" + ctx.booking_reference)


class TestPipelineAdapter(SpaBookingAdapter):
    @staticmethod
    def _select_delegate(spa):
        # No decrypting credentials, OAuth, Square client, network or fallback.
        return _TestProvider()

    async def check_availability(self, ctx):
        ctx, reason = self._staff_context(ctx)
        if reason:
            return AvailabilityVerdict.no(reason)
        return await super().check_availability(ctx)

    async def create_booking(self, ctx):
        ctx, reason = self._staff_context(ctx)
        if reason:
            raise BookingProviderError(reason)
        return await super().create_booking(ctx)


def development_only():
    import os
    # Railway may omit APP_ENV, whose local default is development. Its
    # environment identity must still keep simulator writes out of production.
    railway_environment = os.environ.get("RAILWAY_ENVIRONMENT_NAME", "").strip().lower()
    if settings.APP_ENV == "production" or railway_environment in {"production", "prod"}:
        raise HTTPException(403, "The shared-pipeline test runs only on a development backend/database.")


def snapshot(spa):
    return SimpleNamespace(id=spa.id, name=spa.name, timezone=spa.timezone,
                           services=spa.services, staff=spa.staff, business_hours=spa.business_hours,
                           payment_policy={"card_required": False, "collection_mode": "none"},
                           notification_settings={}, booking_policies=spa.booking_policies)


async def test_routing(db, session, adapter):
    from app.services.appointment_booking_service import _Routing, _capacity
    development_only()
    if type(adapter) is not TestPipelineAdapter or not session.call_sid.startswith("cara-test:") or not session.tenant_id:
        raise HTTPException(403, "Invalid campaign test adapter or scope.")
    tid = uuid.UUID(session.tenant_id)
    contact = (await db.execute(select(Contact).where(Contact.tenant_id == tid, Contact.phone_number == session.customer_phone))).scalar_one_or_none()
    if not contact or (contact.extra_metadata or {}).get("is_test_customer") is not True or adapter.spa.id != tid:
        raise HTTPException(403, "Shared test booking requires this establishment's marked test contact.")
    # Test snapshots intentionally disable notifications/payment collection.
    # This does not change the real business's saved settings or card policy.
    session.entities["card_on_file_required"] = False
    return _Routing(session.scope, adapter.spa, adapter, _capacity(adapter.spa), False, "Cara campaign TEST")


async def stage(db, tenant, actor, delivery_id, start, customer_name, preferred_staff=None):
    from app.services import cara_manager as m
    from app.services.appointment_booking_service import stage_booking, BookingOutcome
    development_only()
    delivery = await m.load_delivery(db, tenant, delivery_id)
    campaign = await m.load_campaign(db, tenant, delivery.campaign_id)
    await m.check_approval(db, campaign)
    contact = await db.get(Contact, delivery.contact_id)
    if not campaign.proposal["test_mode"] or (contact.extra_metadata or {}).get("is_test_customer") is not True or not m.consent(contact):
        raise HTTPException(403, "Only a consenting test customer can enter this flow.")
    if delivery.booking:
        raise HTTPException(409, "This delivery already has a booking result.")
    if delivery.status != "responded_test" or (delivery.response or "").strip().upper() not in {"YES", "YES PLEASE", "BOOK"}:
        raise HTTPException(409, "A customer must first request an appointment in reply to outreach.")
    spa = await db.get(SpaAccount, tenant)
    from app.services.scheduling_time import business_zone, localize_wall_time, elapsed_end
    try:
        start = localize_wall_time(start, business_zone(spa.timezone))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if start <= m.now_utc():
        raise HTTPException(422, "Choose a future time with an explicit timezone.")
    if not customer_name.strip() or m.sensitive_health_request(customer_name) or m.contains_sensitive_payment(customer_name):
        raise HTTPException(422, "Supply the test customer's name without sensitive details.")
    offer = next(p["offer"] for p in campaign.proposal["audience"] if p["contact_id"] == str(contact.id))
    session = CallSession(call_sid="cara-test:" + str(delivery.id), direction="inbound",
                          from_number=contact.phone_number, to_number="test-local",
                          tenant_id=str(tenant), timezone=spa.timezone)
    session.add_turn("user", "My name is " + customer_name.strip())
    session.entities["requested_local_start"] = start.astimezone(business_zone(spa.timezone)).strftime("%Y-%m-%dT%H:%M")
    adapter = TestPipelineAdapter(snapshot(spa))
    intent = AppointmentIntent(intent="schedule", caller_name=customer_name.strip(),
                               service_description=f"{offer['name']} ({offer['duration_minutes']} min)",
                               requested_start_iso=start.isoformat(),
                               requested_end_iso=elapsed_end(start, offer["duration_minutes"]).isoformat(),
                               preferred_staff=preferred_staff, confidence=1)
    result = await stage_booking(db, session, intent, test_adapter=adapter)
    if result.outcome != BookingOutcome.DRAFT:
        delivery.booking_session = None
        m.audit(db, tenant, actor, "test_pipeline_slot_rejected", campaign.id, delivery_id=str(delivery.id))
        await db.commit()
        raise HTTPException(409, result.message)
    draft = mark_read_back(session)
    delivery.booking_session = session.to_dict()
    m.audit(db, tenant, actor, "test_pipeline_slot_proposed", campaign.id, delivery_id=str(delivery.id), fingerprint=proposal_fingerprint(draft))
    await db.commit()
    return {"status": "awaiting_customer_confirmation", "simulated": True,
            "fingerprint": proposal_fingerprint(draft), "slot": draft.selected_slot,
            "message": result.message, "business_card_required": bool((spa.payment_policy or {}).get("card_required")),
            "card_collection": "Test flow does not collect payments or cards. Existing secure Square collection remains required for live bookings."}


async def confirm(db, tenant, actor, delivery_id, fingerprint, confirmation):
    from app.services import cara_manager as m
    from app.services.appointment_booking_service import confirm_booking, BookingOutcome
    development_only()
    delivery = await m.load_delivery(db, tenant, delivery_id)
    campaign = await m.load_campaign(db, tenant, delivery.campaign_id)
    if not campaign.proposal["test_mode"]:
        raise HTTPException(403, "Test flow only")
    if delivery.booking:
        return delivery.booking
    await m.check_approval(db, campaign)
    contact = (await db.execute(select(Contact).where(Contact.id == delivery.contact_id, Contact.tenant_id == tenant).with_for_update())).scalar_one()
    if (contact.extra_metadata or {}).get("is_test_customer") is not True or not m.consent(contact):
        raise HTTPException(403, "Test customer no longer has contact permission.")
    if not delivery.booking_session:
        raise HTTPException(409, "Select and read back a test appointment before confirming.")
    if m.contains_sensitive_payment(confirmation) or m.sensitive_health_request(confirmation):
        raise HTTPException(422, "Do not include sensitive information in a confirmation.")
    session = CallSession.from_dict(deepcopy(delivery.booking_session))
    draft = get_draft(session)
    if fingerprint != proposal_fingerprint(draft) or not record_pure_confirmation(session, confirmation):
        raise HTTPException(409, "The current exact appointment needs a pure customer confirmation.")
    # Recover the committed appointment if a response failed after the shared
    # core committed but before this delivery's result was recorded.
    existing = (await db.execute(select(Appointment).where(Appointment.tenant_id == tenant,
                           Appointment.booking_intent_key == intent_key(session, draft)))).scalar_one_or_none()
    if existing:
        if existing.booking_provider != "test_pipeline" or not existing.external_booking_id:
            raise HTTPException(409, "The recovered appointment is not a successful test booking.")
        appointment = existing
    else:
        upcoming = (await db.execute(select(Appointment.id).where(Appointment.tenant_id == tenant, Appointment.contact_id == contact.id,
                            Appointment.status.in_(["scheduled", "confirmed"]), Appointment.end_time > m.now_utc()).limit(1))).first()
        if upcoming:
            raise HTTPException(409, "The test customer already has an upcoming appointment.")
        spa = await db.get(SpaAccount, tenant)
        result = await confirm_booking(db, session, test_adapter=TestPipelineAdapter(snapshot(spa)))
        if result.outcome != BookingOutcome.BOOKED or not result.appointment or not result.appointment.external_booking_id:
            raise HTTPException(409, result.message or "The shared booking pipeline did not report success.")
        appointment = result.appointment
    slot = draft.selected_slot or {}
    delivery.booking = {"provider": "test_pipeline", "external_booking_id": appointment.external_booking_id,
                        "appointment_id": str(appointment.id), "start": m.aware(appointment.start_time).isoformat(),
                        "duration_minutes": int((appointment.end_time-appointment.start_time).total_seconds()/60),
                        "team_member_id": slot.get("team_member_id"), "simulated": True, "revenue_verified": False,
                        "card_collection": "Not exercised by the local test adapter"}
    delivery.booking_session = session.to_dict()
    delivery.status = "booked_test"
    m.audit(db, tenant, actor, "test_pipeline_booking_confirmed", campaign.id, delivery_id=str(delivery.id), appointment_id=str(appointment.id))
    await db.commit()
    return delivery.booking
