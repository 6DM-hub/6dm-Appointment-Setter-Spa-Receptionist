"""Optional read-only recommendations; booking stays in the existing state machine.

Replacement upgrades use one provider catalog variation. Owner-approved append
rules use the Square adapter's atomic, resource-verified multi-service visit
search and never mutate the original booking while previewing the addition.
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

def ranked_rules(
    config,
    base,
    completed_services=(),
    prior_offers=(),
    consultation_addons=(),
):
    """Owner-approved compatibility first, history is evidence, never medical inference."""
    if not config.enabled or not config.max_suggestions or base in config.excluded_services:
        return []
    declined = {x.target_service for x in prior_offers if x.status == "declined"}
    counts = Counter(completed_services) if config.personalize else Counter()
    if config.personalize:
        counts.update(x.target_service for x in prior_offers
                      if x.target_service and (x.status == "booked" or (getattr(x, "facts", None) or {}).get("accepted")))
    already_presented = {
        str(name).strip().casefold() for name in consultation_addons if str(name).strip()
    }
    rules = [r for r in config.rules if r.base_service == base
             and r.target_service != base and r.target_service not in config.excluded_services
             and (not r.requires_resources or r.offer_type == "append")
             and r.target_service.casefold() not in already_presented
             and r.target_service not in declined]
    return sorted(rules, key=lambda r: (-r.priority, -counts[r.target_service], r.target_service))


def _instant(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_SEGMENT_IDENTITY_FIELDS = (
    "service_variation_id",
    "service_variation_version",
    "team_member_id",
    "duration_minutes",
)
_SEGMENT_PROVIDER_FIELDS = (
    "intermission_minutes",
    "transition_time_minutes",
    "any_team_member",
)


def _existing_segments(slot):
    """Return the provider-pinned services already accepted by the caller."""
    segments = slot.get("visit_segments") or []
    if segments:
        return segments
    start = _instant(slot.get("start"))
    minutes = slot.get("duration_minutes")
    if (
        start is None
        or not isinstance(minutes, int)
        or isinstance(minutes, bool)
        or minutes <= 0
    ):
        return []
    return [{
        "duration_minutes": minutes,
        "service_variation_id": slot.get("service_variation_id"),
        "service_variation_version": slot.get("service_variation_version"),
        "team_member_id": slot.get("team_member_id"),
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=minutes)).isoformat(),
    }]


def _same_segment(actual, expected):
    if any(actual.get(key) != expected.get(key) for key in _SEGMENT_IDENTITY_FIELDS):
        return False
    if _instant(actual.get("start")) != _instant(expected.get("start")):
        return False
    if _instant(actual.get("end")) != _instant(expected.get("end")):
        return False
    if expected.get("service_name") and actual.get("service_name") != expected.get("service_name"):
        return False
    for key in _SEGMENT_PROVIDER_FIELDS:
        if key in expected and actual.get(key) != expected.get(key):
            return False
    if "resource_ids" in expected:
        if set(actual.get("resource_ids") or []) != set(expected.get("resource_ids") or []):
            return False
    return True


def _verified_append_slot(slot, original, target):
    """Verify the exact existing visit plus one provider-returned terminal service."""
    expected = _existing_segments(original)
    segments = slot.get("visit_segments") or []
    if (
        not expected
        or len(segments) != len(expected) + 1
        or slot.get("location_id") != original.get("location_id")
        or _instant(slot.get("start")) != _instant(original.get("start"))
    ):
        return False
    if any(not _same_segment(actual, prior) for actual, prior in zip(segments, expected)):
        return False

    terminal = segments[-1]
    if (
        terminal.get("service_variation_id") != target["id"]
        or terminal.get("service_variation_version") != target["version"]
        or terminal.get("duration_minutes") != target["minutes"]
        or not terminal.get("team_member_id")
        or (target.get("name") and terminal.get("service_name") != target["name"])
    ):
        return False

    start = _instant(slot.get("start"))
    terminal_start = _instant(terminal.get("start"))
    terminal_end = _instant(terminal.get("end"))
    original_minutes = original.get("duration_minutes")
    combined_minutes = slot.get("duration_minutes")
    if (
        start is None
        or terminal_start is None
        or terminal_end is None
        or not isinstance(original_minutes, int)
        or isinstance(original_minutes, bool)
        or original_minutes <= 0
        or not isinstance(combined_minutes, int)
        or isinstance(combined_minutes, bool)
        or combined_minutes <= original_minutes
    ):
        return False
    # Preserve every minute the original provider result reserved. The new
    # service may begin later when the provider includes a transition gap.
    if terminal_start < start + timedelta(minutes=original_minutes):
        return False
    if terminal_end != terminal_start + timedelta(minutes=target["minutes"]):
        return False
    if terminal_end > start + timedelta(minutes=combined_minutes):
        return False
    treatment_minutes = slot.get("treatment_minutes")
    if treatment_minutes is not None and treatment_minutes != sum(
        segment.get("duration_minutes") or 0 for segment in segments
    ):
        return False
    # The root fields must continue to identify the first pinned segment. The
    # final exact recheck uses this complete slot without reselecting providers.
    first = segments[0]
    if any(slot.get(key) != first.get(key) for key in _SEGMENT_IDENTITY_FIELDS[:-1]):
        return False
    return True


def _append_gap_minutes(slot, original):
    segments = slot.get("visit_segments") or []
    existing = _existing_segments(original)
    prior_end = _instant(existing[-1].get("end")) if existing else None
    terminal_start = _instant(segments[-1].get("start")) if segments else None
    if prior_end is None or terminal_start is None:
        return None
    return int((terminal_start - prior_end).total_seconds() // 60)

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
    consultation_addons = session.entities.get("consultation_addon_names") or ()
    if not ranked_rules(config, base, consultation_addons=consultation_addons):
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
    rules = ranked_rules(
        config,
        base,
        completed,
        prior,
        consultation_addons=consultation_addons,
    )
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
    target_entry = menu[0]
    target_id = target_entry["square_variation_id"]
    start = _instant(draft.selected_slot.get("start"))
    if start is None:
        state["phase"] = "skipped"
        return None
    if rule.offer_type == "append":
        # The original provider-verified slot remains untouched while this
        # read-only complete-visit lookup runs. Applying one preferred staff
        # name to the combined context would incorrectly require the massage
        # therapist to perform the facial, so the atomic visit resolver applies
        # each service's own configured staff restrictions instead.
        existing_segments = _existing_segments(draft.selected_slot)
        variation_ids = [segment.get("service_variation_id") for segment in existing_segments]
        if not existing_segments or any(not variation_id for variation_id in variation_ids):
            state["phase"] = "skipped"
            return None
        unique_ids = list(dict.fromkeys([*variation_ids, target_id]))
        catalog = await asyncio.gather(*(catalog_fact(delegate, variation_id) for variation_id in unique_ids))
        facts = dict(zip(unique_ids, catalog))
        if any(fact is None for fact in facts.values()):
            state["phase"] = "skipped"
            return None
        for segment in existing_segments:
            fact = facts[segment["service_variation_id"]]
            if (
                fact["version"] != segment.get("service_variation_version")
                or fact["minutes"] != segment.get("duration_minutes")
            ):
                state["phase"] = "skipped"
                return None
        currencies = {facts[variation_id]["currency"] for variation_id in variation_ids}
        target = {**facts[target_id], "name": rule.target_service}
        currencies.add(target["currency"])
        if len(currencies) != 1:
            state["phase"] = "skipped"
            return None
        original_treatment_minutes = draft.selected_slot.get("treatment_minutes")
        if original_treatment_minutes is not None and original_treatment_minutes != sum(
            segment["duration_minutes"] for segment in existing_segments
        ):
            state["phase"] = "skipped"
            return None
        original_reserved_minutes = draft.selected_slot.get("duration_minutes")
        if (
            not isinstance(original_reserved_minutes, int)
            or isinstance(original_reserved_minutes, bool)
            or original_reserved_minutes <= 0
        ):
            state["phase"] = "skipped"
            return None
        target_buffers = sum(
            int(target_entry.get(key) or 0)
            for key in (
                "preparation_buffer_minutes",
                "transition_buffer_minutes",
                "cleanup_buffer_minutes",
            )
        )
        combined = f"{base} + {rule.target_service}"
        ctx = BookingContext(
            start=start,
            end=start + timedelta(
                minutes=original_reserved_minutes + target["minutes"] + target_buffers
            ),
            title=combined,
            service_description=combined,
            customer_phone=session.customer_phone,
            preferred_staff=None,
            provider_id=None,
        )
        slots = await routing.adapter.list_openings(ctx, start, start + timedelta(seconds=1))
        slot = next(
            (
                candidate
                for candidate in slots
                if _verified_append_slot(candidate, draft.selected_slot, target)
            ),
            None,
        )
        if slot is None:
            state["phase"] = "skipped"
            return None
        extra_minutes = slot["duration_minutes"] - original_reserved_minutes
        if extra_minutes <= 0:
            state["phase"] = "skipped"
            return None
        incremental_minor = target["price"]
        total_minor = sum(facts[variation_id]["price"] for variation_id in variation_ids) + target["price"]
        booked_description = combined
    else:
        if draft.selected_slot.get("visit_segments"):
            state["phase"] = "skipped"
            return None
        base_id = draft.selected_slot.get("service_variation_id")
        if not base_id:
            state["phase"] = "skipped"
            return None
        original_price, target = await asyncio.gather(
            catalog_fact(delegate, base_id), catalog_fact(delegate, target_id)
        )
        if not original_price or not target or original_price["currency"] != target["currency"]:
            state["phase"] = "skipped"
            return None
        if original_price["minutes"] != draft.selected_slot.get("duration_minutes"):
            state["phase"] = "skipped"
            return None
        if target["price"] <= original_price["price"] or target["minutes"] < original_price["minutes"]:
            state["phase"] = "skipped"
            return None
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
        incremental_minor = target["price"] - original_price["price"]
        total_minor = target["price"]
        booked_description = rule.target_service
        extra_minutes = target["minutes"] - original_price["minutes"]
    # Caller may have changed their request during the asynchronous lookup.
    if proposal_fingerprint(get_draft(session)) != state["original_fingerprint"]:
        state["phase"] = "superseded"
        return None
    approaches = ["helpful", "availability", "complementary"]
    previous = (prior[0].facts or {}).get("approach") if prior else None
    approach = "personalized" if rule.target_service in completed and previous != "personalized" else approaches[(approaches.index(previous) + 1) % len(approaches)] if previous in approaches else "helpful"
    state.update(phase="offered", target_service=rule.target_service, target=target,
        target_slot=copy.deepcopy(slot), incremental_minor=incremental_minor,
        total_minor=total_minor, offer_type=rule.offer_type,
        booked_service_description=booked_description,
        extra_minutes=extra_minutes,
        currency=target["currency"], approach=approach)
    leads = {"helpful": "You could also choose", "availability": "There is also room for",
             "complementary": "For a little more time to unwind, you could choose",
             "personalized": "You've booked this treatment before; you could also choose"}
    # No popularity claim: current records do not establish provider-wide popularity.
    if rule.offer_type == "append":
        no_gap = _append_gap_minutes(slot, draft.selected_slot) == 0
        if "massage" in base.casefold() and "facial" in rule.target_service.casefold():
            return (
                "It looks like our esthetician is available "
                f"{'right after' if no_gap else 'following'} your massage. "
                f"If you'd like, we could add {rule.target_service}. No pressure at all. "
                "Would you like to hear the extra price and time?"
            )
        return (
            f"{rule.target_service} is also available "
            f"{'immediately after' if no_gap else 'following'} your treatment. "
            "Would you like to hear the extra price and time?"
        )
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
    details_requested = bool(re.search(
        r"\b(?:how much|what(?:'s| is) the (?:price|cost)|price|cost|"
        r"how long|how many (?:extra )?minutes|extra time|duration)\b",
        normalized,
    ))
    if not is_affirmative(text) and not upgrade_yes and not details_requested:
        if normalized:
            state["phase"] = "superseded"
            return False, None, "superseded", False
        return True, None, None, False

    def price_confirmation_line():
        total = state.get("total_minor", state["target"]["price"]) / 100
        extra = state["incremental_minor"] / 100
        label = "addition" if state.get("offer_type") == "append" else "upgrade"
        return (f"That's {state['currency']} {extra:.2f} extra and {state['extra_minutes']} additional minutes, "
                f"for {state['currency']} {total:.2f} total. Shall I book that {label}?")

    if state["phase"] == "offered":
        state["phase"] = "price_confirmation"
        return True, price_confirmation_line(), None, False
    if details_requested:
        # A natural price/time question is not a rejection or a change to the
        # booking. Keep the verified offer pending and answer from its fixed,
        # provider/catalog-grounded facts.
        return True, price_confirmation_line(), None, False
    draft = get_draft(session)
    draft.service_description = state.get("booked_service_description") or state["target_service"]
    draft.service_id = None
    if state.get("offer_type") == "append":
        draft.duration_minutes = state["target_slot"]["duration_minutes"]
        draft.square_variation_id = state["target_slot"]["service_variation_id"]
        draft.square_variation_version = state["target_slot"]["service_variation_version"]
        # A caller's preference for the original therapist cannot be applied to
        # every service in a multi-provider visit. The pinned provider-returned
        # segments remain the sole source of provider identity on final recheck.
        draft.preferred_staff = None
        draft.provider_id = None
    else:
        draft.duration_minutes = state["target"]["minutes"]
        draft.square_variation_id = state["target"]["id"]
        draft.square_variation_version = state["target"]["version"]
    start = datetime.fromisoformat(state["target_slot"]["start"].replace("Z", "+00:00"))
    draft.start_iso = start.isoformat()
    draft.end_iso = (start + timedelta(minutes=draft.duration_minutes)).isoformat()
    draft.draft_revision += 1
    draft.alternative_slots = []
    draft.verified_availability = None
    save_draft(session, draft)
    bind_verified_slot(session, state["target_slot"])
    session.selected_service = draft.service_description
    session.selected_duration = draft.duration_minutes
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
