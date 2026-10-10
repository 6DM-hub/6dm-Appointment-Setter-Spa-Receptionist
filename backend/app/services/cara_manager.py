"""Evidence-based 90-day reactivation. This increment has no live sender."""
import hashlib
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fastapi import HTTPException
from sqlalchemy import select
from app.models import Appointment, AppointmentStatus, CallLog, Contact, SpaAccount, User, UserRole
from app.models.cara_manager import CaraAudit, CaraCampaign, CaraDelivery, CaraPackage, CaraPreference
from app.services.secure_payment import contains_sensitive_payment
from app.services.sensitive_information import sensitive_health_request

DEFAULT_PREFERENCES = {"discount_limit_pct": 0, "services_to_promote": [], "appointment_priority": "earliest_available",
                       "discount_excluded_weekdays": [], "promotion_until": None}
CAPABILITIES = {
    "booking": "Existing Square/Google booking adapters; provider success required",
    "customer_history": "Locally recorded completed appointments; external history is not imported",
    "gift_card_balances": "unsupported by current adapters; unknown",
    "membership_benefits": "unsupported by current adapters; unknown",
    "external_package_creation": "unsupported by current adapters; approved local offers only",
    "live_campaign_delivery": "disabled; test outbox only",
    "capacity": "not promised in a campaign; checked at booking",
    "test_booking": "Development-only shared appointment pipeline with local test adapter; not external provider success",
    "medical_office": "disabled pending technical and contractual verification; no HIPAA-compliance claim",
}


async def integration_report(db, tenant):
    from app.models import BookingProvider
    from app.services.booking_adapters.providers import VERTICAL_PROVIDERS
    from app.services.booking_config import decrypt_config, missing_config
    from app.core.config import settings
    spa = await db.get(SpaAccount, tenant)
    if not spa:
        raise HTTPException(404, "Establishment not found")
    provider = spa.booking_provider
    cls = VERTICAL_PROVIDERS.get(provider)
    implemented = provider == BookingProvider.GOOGLE_CALENDAR or bool(cls and cls.implemented)
    missing = missing_config(provider, spa.booking_config)
    ready = implemented and not missing
    directory = ready and bool(cls and cls.supports_customer_lookup)
    cards = ready and bool(cls and cls.supports_save_card_on_file)
    config = decrypt_config(spa.booking_config)
    return {"selected_booking_system": provider.value, "adapter_implemented": implemented,
            "configuration_status": "configured_not_live_checked" if ready else "incomplete_or_unsupported",
            "missing_configuration_fields": missing,
            "bookings_authority": provider.value if ready else None,
            "local_records": "Cara database stores dashboard records and completed-visit evidence; not external success",
            "customer_directory_authority": provider.value if directory else "Cara local contacts only",
            "balances_authority": None, "memberships_authority": None,
            "secure_card_supported": cards, "charges_or_deposits_implemented": False,
            "square_environment": config.get("environment", getattr(settings, "SQUARE_ENVIRONMENT", "production")) if provider == BookingProvider.SQUARE else None,
            "campaign_delivery": "test outbox only; live delivery disabled",
            "campaign_booking": "development-only local test adapter; external campaign booking not enabled",
            "medical_office": "disabled", "live_connection_checked": False,
            "payment_compatibility_note": "Square Bookings API does not support booking catalog services with nonzero no_show_fee; verify the service and policy before fee/deposit implementation" if provider == BookingProvider.SQUARE else "Payment capabilities must be verified per provider"}


def now_utc():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def consent(contact):
    metadata = contact.extra_metadata or {}
    return metadata.get("marketing_sms_opt_in") is True and not (
        metadata.get("sms_opt_out") or metadata.get("do_not_contact")
    )


def business_master(user, tenant):
    return bool(user and user.is_active and user.role == UserRole.SPA_ADMIN
                and user.tenant_id == tenant and user.is_business_master is True)


async def interpret(db, tenant, message):
    if contains_sensitive_payment(message):
        return {"kind": "sensitive_information", "blocked": True,
                "message": "Do not enter card details here. Use the establishment's supported secure collection process."}
    if sensitive_health_request(message):
        return {"kind": "sensitive_information", "blocked": True, "sensitive_health_request": True,
                "message": "This may involve sensitive health information. Medical-office workflows are disabled pending technical and contractual verification. Do not include patient details here."}
    spa = await db.get(SpaAccount, tenant)
    if not spa:
        raise HTTPException(404, "Establishment not found")
    text = message.strip()
    prefs = dict(DEFAULT_PREFERENCES, **await preferences(db, tenant))
    if re.search(r"(?:do not|don't|never) discount", text, re.I):
        weekdays = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"}
        days = [code for name, code in weekdays.items() if re.search(rf"\b{name}s?\b", text, re.I)]
        if not days:
            return {"kind": "clarification", "message": "Which weekdays should have no discounts? Nothing has been saved."}
        prefs["discount_excluded_weekdays"] = sorted(set(prefs["discount_excluded_weekdays"]) | set(days))
        return {"kind": "proposed_preference", "preferences": prefs,
                "message": "Review these weekday discount restrictions and confirm to save. Nothing has been saved yet."}
    promote = re.search(r"\bpromote (.+?)(?: this month|$)", text, re.I)
    if promote:
        requested = promote.group(1).strip(" .?!").casefold().rstrip("s")
        names = sorted({str(s.get("name", "")) for s in spa.services or [] if requested in str(s.get("name", "")).casefold()})
        if len(names) != 1:
            return {"kind": "clarification", "options": names,
                    "message": "Select the exact service to promote. Nothing has been saved."}
        prefs["services_to_promote"] = names
        if "this month" in text.casefold():
            from zoneinfo import ZoneInfo
            local = now_utc().astimezone(ZoneInfo(spa.timezone))
            next_month = local.replace(year=local.year+1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0) if local.month == 12 else local.replace(month=local.month+1, day=1, hour=0, minute=0, second=0, microsecond=0)
            prefs["promotion_until"] = next_month.astimezone(timezone.utc).isoformat()
        else:
            prefs["promotion_until"] = None
        return {"kind": "proposed_preference", "preferences": prefs,
                "message": "Review the service and promotion expiry, then confirm to save. No campaign has been created or sent."}
    if re.search(r"\b(holiday|valentine|christmas|fill.*openings)\b", text, re.I):
        return {"kind": "proposed_change", "supported": False,
                "message": "This needs capacity-aware package or opening-fill support, which is in the project backlog. Nothing was created or sent."}
    if re.search(r"reactivat|90\s*[- ]?day|bring.*back|inactive", text, re.I):
        return {"kind": "campaign_proposal", "supported": True,
                "message": "Prepare a 90-day test proposal for review. This does not authorize outreach."}
    if re.search(r"\b(send|execute|launch|run|book|cancel|reschedule)\b", text, re.I):
        return {"kind": "action_request", "authorized": False,
                "message": "Specify the exact task or proposal. Execution requires the appropriate review, permissions and approval; nothing has been executed."}
    if "?" in text or re.match(r"(?:what|how|show|tell|list|when|where|which)\b", text, re.I):
        from app.services.spa_facts import dashboard_facts
        return {"kind": "question", "facts": dashboard_facts(spa),
                "message": "Here are this establishment's configured facts. Missing values are unknown; no changes were made."}
    return {"kind": "clarification", "message": "Is this a question, a preference to save, or a proposal to prepare? Nothing has changed."}


def insight(contact, appointments, now):
    completed = sorted(
        [a for a in appointments if a.contact_id == contact.id
         and a.status == AppointmentStatus.COMPLETED and aware(a.end_time) <= now],
        key=lambda a: aware(a.end_time),
    )
    upcoming = [a for a in appointments if a.contact_id == contact.id
                and a.status in (AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED)
                and aware(a.end_time) > now]
    # A past scheduled row is not proof of a completed visit.
    counts = Counter((a.title, int((aware(a.end_time) - aware(a.start_time)).total_seconds() / 60))
                     for a in completed)
    last = aware(completed[-1].end_time) if completed else None
    intervals = [(aware(b.end_time) - aware(a.end_time)).days
                 for a, b in zip(completed, completed[1:])]
    metadata = contact.extra_metadata or {}
    return {
        "contact_id": str(contact.id), "customer": contact.full_name,
        "source": "test_fixture" if metadata.get("is_test_customer") is True else "local_completed_appointments",
        "completed_visits": len(completed), "last_completed_at": last.isoformat() if last else None,
        "days_since_visit": (now - last).days if last else None,
        "average_days_between_visits": round(sum(intervals) / len(intervals), 1) if intervals else None,
        "favorite_services": [{"name": name, "duration_minutes": minutes, "completed_count": count,
                               "preference_type": "inferred_from_completed_visits"}
                              for (name, minutes), count in counts.most_common(3)],
        "confirmed_preferences": metadata.get("confirmed_preferences", {}),
        "preferred_provider": None,
        "provider_evidence": "Provider identity is not stored on historical local appointment rows",
        "gift_card_balance": None, "membership_benefits": None,
        "engagement": None, "has_upcoming_appointment": bool(upcoming),
        "marketing_sms_allowed": consent(contact),
        "eligible": bool(last and last <= now - timedelta(days=90) and not upcoming and consent(contact)),
    }


def menu_snapshot(spa):
    """Quote owner-maintained prices exactly; never parse a range as a price."""
    rows = []
    for item in spa.services or []:
        if item.get("is_active") is False:
            continue
        price = str(item.get("price") or "").strip()
        if not re.fullmatch(r"\$?\d+(?:\.\d{1,2})?", price):
            continue
        try:
            amount = int(Decimal(price.lstrip("$")) * 100)
        except InvalidOperation:
            continue
        minutes = item.get("duration_minutes")
        if amount <= 0 or not isinstance(minutes, int) or minutes <= 0:
            continue
        rows.append({"name": str(item.get("name") or ""), "duration_minutes": minutes,
                     "price_minor": amount, "currency": "USD",
                     "price_source": "owner_configured_service_menu"})
    return rows


async def preferences(db, tenant):
    row = (await db.execute(select(CaraPreference).where(CaraPreference.tenant_id == tenant))).scalar_one_or_none()
    return dict(row.value) if row else dict(DEFAULT_PREFERENCES)


def audit(db, tenant, actor, action, campaign=None, **details):
    db.add(CaraAudit(tenant_id=tenant, actor_id=actor, campaign_id=campaign, action=action, details=details))


async def evidence(db, tenant, now=None):
    now = now or now_utc()
    contacts = list((await db.execute(select(Contact).where(Contact.tenant_id == tenant))).scalars())
    appointments = list((await db.execute(select(Appointment).where(Appointment.tenant_id == tenant))).scalars())
    calls = list((await db.execute(select(CallLog.contact_id, CallLog.direction, CallLog.status,
                                         CallLog.started_at).where(CallLog.tenant_id == tenant))).all())
    rows = []
    for c in contacts:
        info = insight(c, appointments, now)
        recorded = [call for call in calls if call.contact_id == c.id]
        if recorded:
            dates = [aware(call.started_at) for call in recorded if call.started_at]
            info["engagement"] = {"source": "recorded_phone_calls", "call_count": len(recorded),
                                  "last_call_at": max(dates).isoformat() if dates else None}
        rows.append(info)
    return contacts, appointments, rows


def build_proposal(spa, contacts, insights, prefs, request, test_mode, schedule, now):
    if not re.search(r"reactivat|90\s*[- ]?day|bring.*back|inactive", request, re.I):
        raise HTTPException(422, "This first increment supports 90-day reactivation. Ask for a 90-day reactivation campaign.")
    menu = menu_snapshot(spa)
    by_contact = {str(c.id): c for c in contacts}
    audience = []
    excluded = Counter()
    for info in insights:
        c = by_contact[info["contact_id"]]
        if test_mode and (c.extra_metadata or {}).get("is_test_customer") is not True:
            excluded["not_a_test_customer"] += 1
            continue
        if not test_mode and (c.extra_metadata or {}).get("is_test_customer") is True:
            excluded["test_fixture"] += 1
            continue
        if not info["eligible"]:
            excluded["no_completed_visit_90_days_ago_upcoming_or_no_permission"] += 1
            continue
        favorites = info["favorite_services"]
        offers = [entry for entry in menu if any(entry["name"].casefold() == f["name"].casefold()
                  and entry["duration_minutes"] == f["duration_minutes"] for f in favorites)]
        promoted = prefs.get("services_to_promote") or []
        expiry = prefs.get("promotion_until")
        if expiry and aware(datetime.fromisoformat(expiry)) <= now:
            promoted = []
        favorite_rank = {(f["name"].casefold(), f["duration_minutes"]): rank for rank, f in enumerate(favorites)}
        offers = sorted(offers, key=lambda entry: (entry["name"] not in promoted if promoted else False,
                        favorite_rank[(entry["name"].casefold(), entry["duration_minutes"])]))
        if not offers:
            excluded["favorite_service_has_no_current_unambiguous_price"] += 1
            continue
        chosen = offers[0]
        # Duplicate owner prices for one named duration are a question, not a guess.
        if len([e for e in menu if e["name"] == chosen["name"] and e["duration_minutes"] == chosen["duration_minutes"]]) != 1:
            excluded["ambiguous_current_menu"] += 1
            continue
        offer = dict(chosen, package_name="Welcome Back " + chosen["name"], services=[chosen["name"]],
                     discount_pct=0, external_package_created=False)
        amount = f"${chosen['price_minor'] / 100:.2f}"
        body = (f"Hi {c.first_name or 'there'}, {spa.name} invites you back for "
                f"{chosen['duration_minutes']} minutes of {chosen['name']} at {amount}. "
                "Reply YES and we'll check available appointments. Reply STOP to opt out.")
        audience.append({"contact_id": str(c.id), "customer": c.full_name,
                         "reason": f"{info['completed_visits']} completed visits; last completed visit {info['days_since_visit']} days ago",
                         "preference_type": "inferred", "offer": offer, "message": body,
                         "channels": ["sms"], "benefits": {"gift_card_balance": None, "membership": None}})
    return {
        "kind": "90_day_reactivation", "test_mode": test_mode, "channel": "sms",
        "schedule": schedule.isoformat(), "timezone": spa.timezone,
        "prepared_at": now.isoformat(), "audience": audience, "excluded": dict(excluded),
        "preferences_hash": digest(prefs), "menu_hash": digest(menu),
        "limitations": list(CAPABILITIES.values()), "discount_pct": 0,
        "capacity": "Check when customer chooses a time; no slot has been reserved",
    }


async def prepare(db, tenant, actor, request, test_mode=True, schedule=None):
    if contains_sensitive_payment(request) or sensitive_health_request(request):
        raise HTTPException(422, "Do not include card details or sensitive health information. Medical-office workflows are not enabled.")
    now = now_utc()
    when = schedule or now
    if when.tzinfo is None:
        raise HTTPException(422, "Schedule needs an explicit timezone.")
    spa = await db.get(SpaAccount, tenant)
    if spa is None:
        raise HTTPException(404, "Spa not found")
    contacts, _, insights = await evidence(db, tenant, now)
    plan = build_proposal(spa, contacts, insights, await preferences(db, tenant), request,
                          test_mode, when.astimezone(timezone.utc), now)
    campaign = CaraCampaign(id=uuid.uuid4(), tenant_id=tenant, requested_by=actor, request=request,
                            proposal=plan, proposal_hash=digest(plan), status="draft")
    db.add(campaign)
    audit(db, tenant, actor, "proposal_prepared", campaign.id,
          audience_count=len(plan["audience"]), test_mode=test_mode, proposal_hash=campaign.proposal_hash)
    await db.commit()
    return campaign


async def load_campaign(db, tenant, campaign_id, lock=False):
    query = select(CaraCampaign).where(CaraCampaign.id == campaign_id, CaraCampaign.tenant_id == tenant)
    if lock:
        query = query.with_for_update()
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Campaign not found")
    return row


async def approve(db, tenant, actor, campaign_id, expected_hash):
    approver = await db.get(User, actor)
    if not business_master(approver, tenant):
        raise HTTPException(403, "Only this establishment's active master account can approve campaigns.")
    campaign = await load_campaign(db, tenant, campaign_id, True)
    if expected_hash != campaign.proposal_hash or digest(campaign.proposal) != expected_hash:
        raise HTTPException(409, "Proposal changed. Review the current proposal before approval.")
    if campaign.status not in ("draft", "approved"):
        raise HTTPException(409, "This campaign has already executed.")
    if not campaign.proposal["audience"]:
        raise HTTPException(422, "No eligible customers with permission and a current priced service.")
    campaign.status = "approved"
    campaign.approved_hash = expected_hash
    campaign.approved_by = actor
    campaign.approved_at = now_utc()
    audit(db, tenant, actor, "master_approved", campaign.id, proposal_hash=expected_hash)
    await db.commit()
    return campaign


async def check_approval(db, campaign):
    if campaign.status not in ("approved", "executed") or not campaign.approved_by:
        raise HTTPException(409, "Master-account approval is required.")
    if campaign.approved_hash != campaign.proposal_hash or digest(campaign.proposal) != campaign.approved_hash:
        raise HTTPException(409, "Material changes require fresh approval.")
    approver = await db.get(User, campaign.approved_by)
    if not business_master(approver, campaign.tenant_id):
        raise HTTPException(409, "Approval requires this establishment's currently authorized master account.")
    spa = await db.get(SpaAccount, campaign.tenant_id)
    if digest(menu_snapshot(spa)) != campaign.proposal["menu_hash"] or digest(await preferences(db, campaign.tenant_id)) != campaign.proposal["preferences_hash"]:
        raise HTTPException(409, "Service prices or preferences changed. Prepare and approve a new proposal.")


async def execute_test(db, tenant, actor, campaign_id):
    from app.services.campaign_booking import development_only
    development_only()
    campaign = await load_campaign(db, tenant, campaign_id, True)
    await check_approval(db, campaign)
    if campaign.proposal["test_mode"] is not True:
        raise HTTPException(403, "Live campaigns are disabled. Only approved test-customer campaigns can execute.")
    if campaign.status == "executed":
        return campaign
    now = now_utc()
    if now < datetime.fromisoformat(campaign.proposal["schedule"]):
        raise HTTPException(409, "The approved schedule is not due yet. Run it after that time.")
    if aware(campaign.approved_at) < now - timedelta(hours=24):
        raise HTTPException(409, "Approval expired. Review and approve again.")
    _, _, insights = await evidence(db, tenant, now)
    eligible = {i["contact_id"] for i in insights if i["eligible"]}
    for recipient in sorted(campaign.proposal["audience"], key=lambda x: x["contact_id"]):
        cid = uuid.UUID(recipient["contact_id"])
        # Lock customer as well as campaign: concurrent campaigns cannot bypass cooldown.
        contact = (await db.execute(select(Contact).where(Contact.id == cid, Contact.tenant_id == tenant).with_for_update())).scalar_one_or_none()
        if contact is None or (contact.extra_metadata or {}).get("is_test_customer") is not True:
            raise HTTPException(403, "Test campaigns cannot deliver to real customers.")
        recent = (await db.execute(select(CaraDelivery.id).where(
            CaraDelivery.tenant_id == tenant, CaraDelivery.contact_id == cid,
            CaraDelivery.status.in_(["sent_test", "responded_test", "booked_test"]),
            CaraDelivery.created_at >= now - timedelta(days=30)))).first()
        allowed = str(cid) in eligible and consent(contact) and not recent
        delivery = CaraDelivery(tenant_id=tenant, campaign_id=campaign.id, contact_id=cid,
                                status="sent_test" if allowed else "skipped", message=recipient["message"])
        db.add(delivery)
        if allowed:
            db.add(CaraPackage(tenant_id=tenant, campaign_id=campaign.id, contact_id=cid,
                               offer=recipient["offer"]))
        audit(db, tenant, actor, "test_outreach_recorded" if allowed else "outreach_skipped",
              campaign.id, contact_id=str(cid), channel="test_sms_outbox")
    campaign.status = "executed"
    await db.commit()
    return campaign


async def load_delivery(db, tenant, delivery_id):
    item = (await db.execute(select(CaraDelivery).where(
        CaraDelivery.id == delivery_id, CaraDelivery.tenant_id == tenant).with_for_update())).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Delivery not found")
    return item


async def reply_test(db, tenant, actor, delivery_id, message):
    from app.services.campaign_booking import development_only
    development_only()
    if contains_sensitive_payment(message) or sensitive_health_request(message):
        raise HTTPException(422, "Do not include raw card details or sensitive health information in test replies.")
    item = await load_delivery(db, tenant, delivery_id)
    campaign = await load_campaign(db, tenant, item.campaign_id)
    if not campaign.proposal["test_mode"]:
        raise HTTPException(403, "Test reply only")
    if item.status not in ("sent_test", "responded_test", "booked_test", "opted_out"):
        raise HTTPException(409, "No test message was delivered.")
    contact = await db.get(Contact, item.contact_id)
    if message.strip().upper() in {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}:
        contact.extra_metadata = dict(contact.extra_metadata or {}, sms_opt_out=True)
        item.status = "opted_out"
    elif item.status != "booked_test" and consent(contact):
        item.status = "responded_test"
    else:
        raise HTTPException(409, "This contact has opted out or has already booked.")
    item.response = message
    audit(db, tenant, actor, "test_reply_recorded", campaign.id, delivery_id=str(item.id), status=item.status)
    await db.commit()
    return item


class TestBookingGateway:
    """Explicit sandbox simulator: zero network calls, zero production calendar writes."""
    async def available(self, offer, start):
        return {"provider": "test", "start": start.isoformat(),
                "duration_minutes": offer["duration_minutes"], "service": offer["name"]}

    async def create(self, slot, key):
        return {"provider": "test", "external_booking_id": "test-" + digest(key)[:24], **slot}


async def book_test(db, tenant, actor, delivery_id, start, gateway=None):
    from app.services.campaign_booking import development_only
    development_only()
    item = await load_delivery(db, tenant, delivery_id)
    campaign = await load_campaign(db, tenant, item.campaign_id)
    if campaign.proposal["test_mode"] is not True:
        raise HTTPException(403, "Live campaign booking is not enabled.")
    if item.booking and item.booking.get("external_booking_id"):
        return item
    await check_approval(db, campaign)
    contact = (await db.execute(select(Contact).where(
        Contact.id == item.contact_id, Contact.tenant_id == tenant).with_for_update())).scalar_one()
    if (contact.extra_metadata or {}).get("is_test_customer") is not True or not consent(contact):
        raise HTTPException(403, "Only consenting test customers can book here.")
    upcoming = (await db.execute(select(Appointment.id).where(
        Appointment.tenant_id == tenant, Appointment.contact_id == contact.id,
        Appointment.status.in_([AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED]),
        Appointment.end_time > now_utc()).limit(1))).first()
    if upcoming:
        raise HTTPException(409, "This customer already has an upcoming appointment.")
    others = (await db.execute(select(CaraDelivery).where(
        CaraDelivery.tenant_id == tenant, CaraDelivery.contact_id == contact.id,
        CaraDelivery.id != item.id, CaraDelivery.booking.is_not(None)))).scalars()
    if any(d.booking and d.booking.get("start") and
           aware(datetime.fromisoformat(d.booking["start"])) > now_utc() for d in others):
        raise HTTPException(409, "This customer already has an upcoming simulated booking.")
    if item.status != "responded_test" or item.response.strip().upper() not in {"YES", "YES PLEASE", "BOOK"}:
        raise HTTPException(409, "Customer must explicitly request a booking after outreach.")
    if start.tzinfo is None:
        from app.services.scheduling_time import business_zone, localize_wall_time
        try:
            start = localize_wall_time(start, business_zone(campaign.proposal["timezone"]))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if start <= now_utc():
        raise HTTPException(422, "Choose a future appointment with an explicit timezone.")
    recipient = next(p for p in campaign.proposal["audience"] if p["contact_id"] == str(item.contact_id))
    gateway = gateway or TestBookingGateway()
    slot = await gateway.available(recipient["offer"], start)
    if not slot:
        raise HTTPException(409, "The selected time is unavailable. Nothing was booked.")
    # A fresh exact recheck precedes create; no booking claim before provider success.
    rechecked = await gateway.available(recipient["offer"], start)
    if rechecked != slot:
        raise HTTPException(409, "Availability changed. Nothing was booked.")
    booking = await gateway.create(slot, str(campaign.id) + ":" + str(item.contact_id))
    if not booking or not booking.get("external_booking_id"):
        raise HTTPException(502, "Booking provider did not confirm success. Nothing is confirmed.")
    if booking.get("provider") != "test":
        raise HTTPException(403, "Test booking cannot write to a real calendar.")
    item.booking = dict(booking, simulated=True, price_minor=recipient["offer"]["price_minor"],
                        revenue_verified=False, card_collection="Existing secure Square process for live bookings; not exercised by this simulator")
    item.status = "booked_test"
    audit(db, tenant, actor, "test_booking_confirmed", campaign.id, delivery_id=str(item.id),
          provider_id=booking["external_booking_id"])
    await db.commit()
    return item


async def results(db, tenant, campaign):
    deliveries = list((await db.execute(select(CaraDelivery).where(
        CaraDelivery.tenant_id == tenant, CaraDelivery.campaign_id == campaign.id))).scalars())
    events = list((await db.execute(select(CaraAudit).where(
        CaraAudit.tenant_id == tenant, CaraAudit.campaign_id == campaign.id).order_by(CaraAudit.created_at))).scalars())
    return {"test_mode": campaign.proposal["test_mode"],
            "messages_sent_test": sum(d.status != "skipped" for d in deliveries),
            "responses": sum(d.response is not None for d in deliveries),
            "appointments_booked_test": sum(bool(d.booking and d.booking.get("external_booking_id")) for d in deliveries),
            "verified_revenue": None, "verified_redemptions": None,
            "deliveries": [{"id": str(d.id), "contact_id": str(d.contact_id), "status": d.status,
                            "message": d.message, "response": d.response, "booking": d.booking,
                            "staged_booking": staged_summary(d)} for d in deliveries],
            "audit": [{"action": e.action, "actor_id": str(e.actor_id) if e.actor_id else None,
                       "at": e.created_at.isoformat(), "details": e.details} for e in events]}


def staged_summary(delivery):
    if not delivery.booking_session:
        return None
    from app.services.booking_state import get_draft, proposal_fingerprint
    from app.services.call_state import CallSession
    from copy import deepcopy
    session = CallSession.from_dict(deepcopy(delivery.booking_session))
    draft = get_draft(session)
    return {"fingerprint": proposal_fingerprint(draft), "slot": draft.selected_slot,
            "requested_local_start": session.entities.get("requested_local_start"),
            "service": draft.service_description, "customer_name": draft.caller_name,
            "preferred_staff": draft.preferred_staff, "awaiting_confirmation": not bool(delivery.booking)}
