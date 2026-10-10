"""Optional read-only recommendations; booking stays in the existing state machine.

Only one provider catalog variation is supported. Separate treatments/resources
fail closed until the adapter exposes an atomic, resource-verified bundle.
"""
from __future__ import annotations
import asyncio
import copy
import hashlib
import hmac
import re
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert
from app.core.config import settings
from app.models import Appointment, AppointmentStatus, Contact, EnhancementOffer
from app.schemas.enhancements import EnhancementSettings
from app.services.booking_state import get_draft, save_draft, proposal_fingerprint, bind_verified_slot, intent_key
from app.services.booking_conversation import remember_offer, accept_offer
from app.services.booking_adapters.base import BookingContext

KEY = "smart_enhancement"

def pending(session):
    return (session.entities.get(KEY) or {}).get("phase") in {"offered", "price_confirmation"}

def customer_key(tenant_id, phone):
    if not phone:
        return None
    return hmac.new(settings.SECRET_KEY.encode(), f"enhancements:{tenant_id}:{phone}".encode(), hashlib.sha256).hexdigest()

def ranked_rules(config, base, completed_services=(), prior_offers=()):
    """Owner-approved compatibility first, history is evidence, never medical inference."""
    if not config.enabled or not config.max_suggestions or base in config.excluded_services:
        return []
    declined = {x.target_service for x in prior_offers if x.status == "declined"}
    counts = Counter(completed_services) if config.personalize else Counter()
    if config.personalize:
        counts.update(x.target_service for x in prior_offers
                      if x.target_service and (x.status == "booked" or (getattr(x, "facts", None) or {}).get("accepted")))
    rules = [r for r in config.rules if r.base_service == base
             and r.target_service != base and r.target_service not in config.excluded_services
             and not r.requires_resources and r.target_service not in declined]
    return sorted(rules, key=lambda r: (-r.priority, -counts[r.target_service], r.target_service))

async def record(db, session, status, **extra):
    state = session.entities.get(KEY) or {}
    if not session.tenant_id or not state:
        return
    values = dict(tenant_id=session.tenant_id, intent_key=state["intent_key"],
        customer_key=state.get("customer_key"), base_service=state["base_service"],
        target_service=state.get("target_service"), status=status,
        incremental_minor=state.get("incremental_minor"), currency=state.get("currency"),
        facts={"approach": state.get("approach"), "accepted": bool(state.get("accepted")),
               "presented": bool(state.get("presented")), "phrase_id": state.get("phrase_id")}, **extra)
    stmt = insert(EnhancementOffer).values(**values)
    # A replay cannot demote a completed write or double-count it.
    await db.execute(stmt.on_conflict_do_update(
        constraint="uq_enhancement_intent", set_={k: v for k, v in values.items()
        if k not in {"tenant_id", "intent_key"}},
        where=EnhancementOffer.status != "booked"))
    await db.commit()

async def catalog_fact(adapter, variation_id):
    """Fetch authoritative fixed price/duration/version; unknown pricing fails closed."""
    if not isinstance(variation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", variation_id):
        return None
    data = await adapter._request("GET", f"/v2/catalog/object/{variation_id}")
    obj = data.get("object") or {}
    v = obj.get("item_variation_data") or {}
    money = v.get("price_money") or {}
    amount, currency, duration = money.get("amount"), money.get("currency"), v.get("service_duration")
    if obj.get("id") != variation_id or obj.get("is_deleted") or v.get("pricing_type") != "FIXED_PRICING":
        return None
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0 or currency not in {"USD", "CAD", "AUD", "NZD", "EUR", "GBP"}:
        return None
    if not isinstance(duration, int) or duration <= 0 or duration % 60000:
        return None
    if not isinstance(obj.get("version"), int) or isinstance(obj.get("version"), bool):
        return None
    return {"id": variation_id, "price": amount, "currency": currency,
            "minutes": duration // 60000, "version": obj.get("version")}

async def prepare(db, session, routing):
    draft = get_draft(session)
    spa = routing.spa
    config = EnhancementSettings.model_validate(getattr(spa, "enhancement_settings", None) or {})
    if not config.enabled or not config.max_suggestions or session.direction != "inbound":
        return None
    if not draft.provider_verified or not draft.selected_slot or draft.is_persisted or draft.operation_mode != "schedule":
        return None
    if str(session.tenant_id) != str(spa.id):
        return None
    # One suggestion per call, including changes to the original request.
    if session.entities.get(KEY):
        return None
    delegate = getattr(routing.adapter, "delegate", routing.adapter)
    if delegate.provider != "square":
        return None
    base = draft.service_description
    if not ranked_rules(config, base):
        return None
    existing_offer = (await db.execute(select(EnhancementOffer.id).where(
        EnhancementOffer.tenant_id == session.tenant_id,
        EnhancementOffer.intent_key.startswith(f"{session.call_sid}:", autoescape=True)
    ).limit(1))).scalar_one_or_none()
    if existing_offer:
        return None
    key = customer_key(session.tenant_id, session.customer_phone)
    profile = (await db.execute(select(Contact.extra_metadata).where(
        Contact.tenant_id == session.tenant_id, Contact.phone_number == session.customer_phone
    ).limit(1))).scalar_one_or_none() or {}
    if profile.get("enhancement_opt_out"):
        return None
    if profile.get("personalization_opt_out") or profile.get("do_not_profile"):
        config.personalize = False
        key = None
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.retention_days)
    await db.execute(delete(EnhancementOffer).where(EnhancementOffer.tenant_id == session.tenant_id,
                                                   EnhancementOffer.created_at < cutoff))
    prior = list((await db.execute(select(EnhancementOffer).where(
        EnhancementOffer.tenant_id == session.tenant_id, EnhancementOffer.customer_key == key,
        EnhancementOffer.created_at >= cutoff).order_by(EnhancementOffer.created_at.desc()).limit(100))).scalars()) if key else []
    completed = []
    if config.personalize and key:
        completed = list((await db.execute(select(Appointment.title).join(Contact, Appointment.contact_id == Contact.id).where(
            Appointment.tenant_id == session.tenant_id, Contact.tenant_id == session.tenant_id,
            Contact.phone_number == session.customer_phone, Appointment.status == AppointmentStatus.COMPLETED,
            Appointment.external_booking_id.is_not(None), Appointment.start_time >= cutoff
        ).limit(100))).scalars())
    if any(row.intent_key == intent_key(session, draft) for row in prior):
        return None
    rules = ranked_rules(config, base, completed, prior)
    if not rules:
        return None
    state = {"intent_key": intent_key(session, draft), "customer_key": key,
             "base_service": base, "original": copy.deepcopy(draft.to_dict()),
             "original_fingerprint": proposal_fingerprint(draft), "phase": "checking"}
    session.entities[KEY] = state
    await record(db, session, "eligible")
    # Strict one-candidate budget. No hunting across provider openings on this path.
    rule = rules[0]
    menu = [e for e in spa.services if e.get("name") == rule.target_service]
    if len(menu) != 1 or not menu[0].get("square_variation_id"):
        state["phase"] = "skipped"
        return None
    target_id = menu[0]["square_variation_id"]
    base_id = draft.selected_slot.get("service_variation_id")
    if not base_id:
        state["phase"] = "skipped"
        return None
    original_price, target = await asyncio.gather(catalog_fact(delegate, base_id), catalog_fact(delegate, target_id))
    if not original_price or not target or original_price["currency"] != target["currency"] or target["price"] <= original_price["price"]:
        state["phase"] = "skipped"
        return None
    if original_price["minutes"] != draft.selected_slot.get("duration_minutes") or target["minutes"] < original_price["minutes"]:
        state["phase"] = "skipped"
        return None
    start = datetime.fromisoformat(draft.selected_slot["start"].replace("Z", "+00:00"))
    ctx = BookingContext(start=start, end=start + timedelta(minutes=target["minutes"]),
        title=rule.target_service, service_description=rule.target_service,
        customer_phone=session.customer_phone, preferred_staff=draft.preferred_staff,
        provider_id=draft.selected_slot.get("team_member_id"), service_variation_id=target_id,
        service_variation_version=target["version"])
    verdict = await routing.adapter.check_availability(ctx)
    slot = verdict.slot or {}
    if (not verdict.available or slot.get("service_variation_id") != target_id
        or slot.get("team_member_id") != draft.selected_slot.get("team_member_id")
        or slot.get("location_id") != draft.selected_slot.get("location_id")
        or slot.get("duration_minutes") != target["minutes"]
        or slot.get("service_variation_version") != target["version"]
        or not slot.get("start")
        or datetime.fromisoformat(slot["start"].replace("Z", "+00:00")) != start):
        state["phase"] = "skipped"
        return None
    # Caller may have changed their request during the asynchronous lookup.
    if proposal_fingerprint(get_draft(session)) != state["original_fingerprint"]:
        state["phase"] = "superseded"
        return None
    approaches = ["helpful", "availability", "complementary"]
    previous = (prior[0].facts or {}).get("approach") if prior else None
    approach = "personalized" if rule.target_service in completed and previous != "personalized" else approaches[(approaches.index(previous) + 1) % len(approaches)] if previous in approaches else "helpful"
    state.update(phase="offered", target_service=rule.target_service, target=target,
        target_slot=copy.deepcopy(slot), incremental_minor=target["price"] - original_price["price"],
        extra_minutes=target["minutes"] - original_price["minutes"], currency=target["currency"], approach=approach)
    leads = {"helpful": "You could also choose", "availability": "There is also room for",
             "complementary": "For a little more time to unwind, you could choose",
             "personalized": "You've booked this treatment before; you could also choose"}
    # No popularity claim: current records do not establish provider-wide popularity.
    if rule.phrase_variants:
        previous_ids = [(row.facts or {}).get("phrase_id") for row in prior]
        variants = sorted(rule.phrase_variants, key=lambda text: (
            hashlib.sha256(text.encode()).hexdigest() in previous_ids,
            -previous_ids.index(hashlib.sha256(text.encode()).hexdigest()) if hashlib.sha256(text.encode()).hexdigest() in previous_ids else 0,
            text))
        phrase = variants[0]
        state["phrase_id"] = hashlib.sha256(phrase.encode()).hexdigest()
        return phrase.replace("{service}", rule.target_service)
    return f"{leads[approach]} {rule.target_service}. Would you like to hear the extra price and time?"

def respond(session, text):
    """Returns (handled, line, status, continue_original). No silent consent."""
    from app.services.booking_state import is_affirmative, BookingDraft
    state = session.entities.get(KEY) or {}
    if not pending(session):
        return False, None, None, False
    if proposal_fingerprint(get_draft(session)) != state["original_fingerprint"]:
        state["phase"] = "superseded"
        return False, None, "superseded", False
    normalized = text.strip().casefold().rstrip(".!?")
    if normalized in {"just book the original", "please book the original", "no, book the original", "no thanks, book the original"}:
        state["phase"] = "declined"
        remember_offer(session)
        accept_offer(session)
        return True, None, "declined", True
    if normalized in {"no", "no thanks", "no thank you", "just the original", "keep the original"}:
        state["phase"] = "declined"
        # Declining a suggestion alone is not consent to create the base booking.
        return True, "Of course, we'll keep your original treatment. Would you like me to book it?", "declined", False
    target = state.get("target_service", "").casefold()
    upgrade_yes = normalized in {"yes upgrade it", "yes, upgrade it", "please upgrade it", "i'll take the upgrade", "i would like the upgrade", f"yes, {target}", f"i'll take {target}", f"{target} please"}
    if not is_affirmative(text) and not upgrade_yes:
        if normalized:
            state["phase"] = "superseded"
            return False, None, "superseded", False
        return True, None, None, False
    if state["phase"] == "offered":
        state["phase"] = "price_confirmation"
        total = state["target"]["price"] / 100
        extra = state["incremental_minor"] / 100
        return True, (f"That's {state['currency']} {extra:.2f} extra and {state['extra_minutes']} additional minutes, "
                      f"for {state['currency']} {total:.2f} total. Shall I book that upgrade?"), None, False
    draft = get_draft(session)
    draft.service_description = state["target_service"]
    draft.service_id = None
    draft.duration_minutes = state["target"]["minutes"]
    draft.square_variation_id = state["target"]["id"]
    draft.square_variation_version = state["target"]["version"]
    start = datetime.fromisoformat(state["target_slot"]["start"].replace("Z", "+00:00"))
    draft.start_iso = start.isoformat()
    draft.end_iso = (start + timedelta(minutes=state["target"]["minutes"])).isoformat()
    draft.draft_revision += 1
    draft.alternative_slots = []
    draft.verified_availability = None
    save_draft(session, draft)
    bind_verified_slot(session, state["target_slot"])
    remember_offer(session)
    accept_offer(session)
    state.update(phase="accepted", accepted=True, upgrade_fingerprint=proposal_fingerprint(get_draft(session)))
    return True, None, "accepted", True

def metrics(rows):
    result = {"eligible": len(rows), "presented": 0, "accepted": 0, "declined": 0,
              "booked": 0, "incremental_booking_value_minor": {}, "realized_revenue": None,
              "by_service": []}
    groups = {}
    for row in rows:
        facts = row.facts or {}
        group = groups.setdefault((row.base_service, row.target_service), {"base_service": row.base_service,
            "enhancement": row.target_service, "presented": 0, "accepted": 0, "declined": 0, "booked": 0,
            "incremental_booking_value_minor": {}})
        for key, flag in [("presented", facts.get("presented")), ("accepted", facts.get("accepted")),
                          ("declined", row.status == "declined"), ("booked", row.status == "booked" and bool(row.external_booking_id))]:
            if flag:
                result[key] += 1
                group[key] += 1
        if row.status == "booked" and row.external_booking_id and row.incremental_minor is not None:
            values = result["incremental_booking_value_minor"]
            values[row.currency] = values.get(row.currency, 0) + row.incremental_minor
            per_service = group["incremental_booking_value_minor"]
            per_service[row.currency] = per_service.get(row.currency, 0) + row.incremental_minor
    result["by_service"] = list(groups.values())
    result["acceptance_rate"] = result["accepted"] / result["presented"] if result["presented"] else None
    return result
