"""Authoritative spa-fact lookup for the receptionist.

The LLM never supplies these values. Dashboard copy is used for business
information the spa configured; the active booking adapter supplies
operational location/timezone when it can. Missing fields are UNKNOWN.
"""
from __future__ import annotations

from typing import Any

from app.models.spa_account import SpaAccount
from app.services.business_hours import describe_business_hours
from app.services.truth_log import truth

UNKNOWN = "I don't have that information available."

DEFAULT_PAYMENT_POLICY = {
    "card_required": False,
    "collection_mode": "none",
    "script": (
        "We do not collect credit-card numbers over the phone. "
        "If a card is required, it is taken at the spa or through a Square-hosted link."
    ),
}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def format_square_address(location: dict[str, Any] | None) -> str | None:
    if not location:
        return None
    addr = location.get("address") if isinstance(location.get("address"), dict) else {}
    parts = [
        addr.get("address_line_1"),
        addr.get("address_line_2"),
        addr.get("locality"),
        addr.get("administrative_district_level_1"),
        addr.get("postal_code"),
        addr.get("country"),
    ]
    formatted = ", ".join(str(part).strip() for part in parts if part and str(part).strip())
    return formatted or None


def payment_policy_of(spa: SpaAccount) -> dict[str, Any]:
    policy = {**DEFAULT_PAYMENT_POLICY, **_as_dict(getattr(spa, "payment_policy", None))}
    policy["card_required"] = bool(policy.get("card_required"))
    mode = str(policy.get("collection_mode") or "none").lower()
    if mode not in {"none", "at_spa", "square_link", "secure_voice_card", "secure_sms_link"}:
        mode = "none"
    policy["collection_mode"] = mode
    return policy


def dashboard_facts(spa: SpaAccount) -> dict[str, Any]:
    services = []
    for entry in spa.services or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        item = {"name": name, "source": "dashboard"}
        if entry.get("duration_minutes"):
            item["duration_minutes"] = entry["duration_minutes"]
        if entry.get("price"):
            item["price"] = str(entry["price"])
        if entry.get("description"):
            item["description"] = str(entry["description"])
        services.append(item)

    hours = describe_business_hours(spa.business_hours, spa.timezone)
    hours_configured = bool(spa.business_hours)

    return {
        "spa_name": spa.name,
        "dashboard_location": (spa.location or "").strip() or None,
        "public_phone": (getattr(spa, "public_phone", None) or "").strip() or None,
        "inbound_phone": spa.twilio_phone_number,
        "description": (getattr(spa, "description", None) or "").strip() or None,
        "cancellation_policy": (getattr(spa, "cancellation_policy", None) or "").strip() or None,
        "amenities": [str(item).strip() for item in _as_list(getattr(spa, "amenities", None)) if str(item).strip()],
        "packages": _as_list(getattr(spa, "packages", None)),
        "booking_policies": _as_dict(getattr(spa, "booking_policies", None)),
        "upsell_rules": _as_list(getattr(spa, "upsell_rules", None)),
        "payment_policy": payment_policy_of(spa),
        "services": services,
        "hours_text": hours if hours_configured else None,
        "dashboard_timezone": spa.timezone,
        "booking_provider": spa.booking_provider.value if spa.booking_provider else None,
    }


_VIP_KEYS = (
    "name",
    "vip_identifier",
    "eligible_services",
    "eligible_staff",
    "credits",
    "benefits",
    "restrictions",
    "expires_on",
    "booking_limit",
)

_POLICY_KEYS = (
    "cancellation_cutoff",
    "reschedule_cutoff",
    "late_cancellation_fee",
    "deposit_policy",
    "no_show_rule",
    "refund_rule",
    "package_cancellation_rule",
)


def configured_vip(packages: list[Any], identifier: str | None = None) -> dict[str, Any]:
    """VIP comes only from packages the spa marked vip. No match means no VIP behavior."""
    vip_rows = []
    for package in packages:
        if not isinstance(package, dict) or package.get("vip") is not True:
            continue
        row = {key: package[key] for key in _VIP_KEYS if key in package and package[key] not in (None, "", [])}
        if row:
            vip_rows.append(row)
    if not vip_rows:
        return {
            "status": "unknown",
            "verified": False,
            "message": "No VIP package is configured. Do not offer VIP benefits, credits, or a special provider.",
        }
    wanted = (identifier or "").strip().casefold()
    if wanted:
        matched = [
            row for row in vip_rows
            if str(row.get("vip_identifier") or row.get("name") or "").casefold() == wanted
        ]
        if not matched:
            return {
                "status": "unverified",
                "verified": False,
                "message": "That VIP package could not be verified from the spa's configuration.",
            }
        vip_rows = matched
    return {
        "status": "ok",
        "verified": bool(wanted),
        "packages": vip_rows,
        "message": "Use only the VIP fields returned here. Do not add benefits, prices, staff, or credits.",
    }


def policy_statement(facts: dict[str, Any]) -> dict[str, Any]:
    """Informational policy only. Missing fields stay unknown and do not decide a cancellation."""
    structured = facts.get("booking_policies") if isinstance(facts.get("booking_policies"), dict) else {}
    known = {
        key: structured[key]
        for key in _POLICY_KEYS
        if key in structured and structured[key] not in (None, "")
    }
    text = facts.get("cancellation_policy")
    if not known and not text:
        return {
            "status": "unknown",
            "informational_only": True,
            "message": UNKNOWN,
        }
    return {
        "status": "ok",
        "informational_only": True,
        "configured": known,
        "cancellation_policy": text,
        "message": text or "Only the configured policy fields may be quoted. Anything not listed is not available.",
    }


def matching_upsells(rules: list[Any], service_name: str | None) -> list[str]:
    if not service_name:
        return []
    wanted = service_name.casefold()
    offered: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        base = str(rule.get("base_service") or "").strip()
        base_cf = base.casefold()
        if not base or (
            base_cf != wanted and base_cf not in wanted and wanted not in base_cf
        ):
            continue
        for item in _as_list(rule.get("allowed_upsells")):
            label = str(item).strip()
            if label and label not in offered:
                offered.append(label)
    return offered


def resolve_location_query(
    query: str | None,
    *,
    location_name: str | None,
    address: str | None,
) -> str:
    """Whether a caller-named branch uniquely matches the configured location."""
    if not query or not query.strip():
        return "match_or_unspecified"
    blob = " ".join(part for part in (location_name, address) if part).casefold()
    tokens = [token for token in query.casefold().replace(",", " ").split() if len(token) > 2]
    if not blob:
        return "unknown"
    if not tokens:
        return "match_or_unspecified"
    hits = sum(1 for token in tokens if token in blob)
    if hits:
        return "match"
    return "unmatched"


async def lookup_spa_facts(
    spa: SpaAccount,
    *,
    adapter: Any | None = None,
    topic: str = "all",
    service_name: str | None = None,
    location_query: str | None = None,
    vip_identifier: str | None = None,
) -> dict[str, Any]:
    """Return only verified fields for the requested topic."""
    facts = dashboard_facts(spa)
    provider_location: dict[str, Any] | None = None
    provider_name = None
    if adapter is not None:
        provider_name = getattr(adapter, "provider", None)
        describe = getattr(adapter, "describe_location", None)
        if callable(describe):
            try:
                provider_location = await describe()
            except Exception:
                provider_location = None

    address = None
    address_source = None
    if provider_location:
        address = format_square_address(provider_location) or str(
            provider_location.get("name") or ""
        ).strip() or None
        if address:
            address_source = "booking_provider"
        timezone = str(provider_location.get("timezone") or "").strip() or facts["dashboard_timezone"]
        location_id = str(provider_location.get("id") or "").strip() or None
        location_name = str(provider_location.get("name") or "").strip() or None
        location_status = str(provider_location.get("status") or "").strip() or None
        truth(
            "LOCATION_VERIFIED",
            provider=provider_name,
            location_id=location_id,
            location_name=location_name,
            timezone=timezone,
            status=location_status,
        )
    else:
        address = facts["dashboard_location"]
        address_source = "dashboard" if address else None
        timezone = facts["dashboard_timezone"]
        location_id = None
        location_name = None
        location_status = None
        if address:
            truth("SPA_DETAIL_LOOKUP", source="dashboard", field="address")

    location_resolution = resolve_location_query(
        location_query, location_name=location_name, address=address
    )

    upsells = matching_upsells(facts["upsell_rules"], service_name)
    payment = facts["payment_policy"]

    payload: dict[str, Any] = {
        "status": "ok",
        "unknown": UNKNOWN,
        "spa_name": facts["spa_name"],
        "booking_provider": provider_name or facts["booking_provider"],
        "topic": topic,
        "location": {
            "address": address,
            "source": address_source,
            "verified": bool(address),
            "location_id": location_id,
            "location_name": location_name,
            "timezone": timezone,
            "status": location_status,
            "caller_location_query": location_query,
            "caller_location_resolution": location_resolution,
            "speak": address or UNKNOWN,
        },
        "phone": facts["public_phone"] or facts["inbound_phone"],
        "description": facts["description"] or UNKNOWN,
        "hours": facts["hours_text"] or UNKNOWN,
        "services": facts["services"],
        "amenities": facts["amenities"] or UNKNOWN,
        "packages": facts["packages"] or UNKNOWN,
        "cancellation_policy": facts["cancellation_policy"] or UNKNOWN,
        "upsells": upsells,
        "upsell_configured": bool(upsells),
        "payment": {
            **payment,
            "collect_raw_card_on_call": False,
            "booking_cc": (
                "Booking CC means the spa's configured card-on-file / payment "
                "requirement for completing a booking. The AI never takes PAN, "
                "expiry, or CVV. collection_mode=none: do not ask for a card. "
                "at_spa: tell the caller a card is handled at the spa if card_required. "
                "square_link: tell them Square will send a secure payment link; "
                "do not collect the number by voice. "
                "secure_voice_card: hand the call to the secure payment flow. "
                "secure_sms_link: after booking, a secure text link may be sent "
                "to save a card on file. Never say a charge was made. "
                "Never ask the caller to speak a card number, expiry, ZIP, or CVV."
            ),
        },
        "vip": configured_vip(facts["packages"]),
        "policies": policy_statement(facts),
    }

    if topic in {"location", "address"}:
        truth("SPA_DETAIL_LOOKUP", source=address_source or "none", field="address")
        if location_resolution == "unmatched":
            payload["status"] = "needs_clarification"
            payload["message"] = (
                "The named location does not match the configured spa location. "
                "Ask the caller to confirm which branch they mean. Do not guess."
            )
        elif not address:
            payload["status"] = "unknown"
            payload["message"] = UNKNOWN
        else:
            payload["message"] = address
        return payload

    if topic in {"hours"}:
        truth("SPA_DETAIL_LOOKUP", source="dashboard" if facts["hours_text"] else "none", field="hours")
        payload["message"] = facts["hours_text"] or UNKNOWN
        payload["status"] = "ok" if facts["hours_text"] else "unknown"
        return payload

    if topic in {"phone"}:
        truth("SPA_DETAIL_LOOKUP", source="dashboard" if payload["phone"] else "none", field="phone")
        payload["message"] = payload["phone"] or UNKNOWN
        payload["status"] = "ok" if payload["phone"] else "unknown"
        return payload

    if topic in {"services"}:
        truth("SPA_DETAIL_LOOKUP", source="dashboard", field="services")
        if not facts["services"]:
            payload["status"] = "unknown"
            payload["message"] = UNKNOWN
        else:
            payload["message"] = "; ".join(item["name"] for item in facts["services"])
        return payload

    if topic in {"prices", "price"}:
        if not service_name:
            payload["status"] = "needs_clarification"
            payload["message"] = "Ask which service they mean before quoting a price."
            return payload
        match = next(
            (
                item
                for item in facts["services"]
                if service_name.casefold() in item["name"].casefold()
                or item["name"].casefold() in service_name.casefold()
            ),
            None,
        )
        price = match.get("price") if match else None
        truth("SPA_DETAIL_LOOKUP", source="dashboard" if price else "none", field="price")
        if not price:
            payload["status"] = "unknown"
            payload["message"] = UNKNOWN
        else:
            payload["message"] = f"{match['name']} is {price}."
            payload["matched_service"] = match
        return payload

    if topic in {"policies", "cancellation"}:
        truth(
            "SPA_DETAIL_LOOKUP",
            source="dashboard" if facts["cancellation_policy"] else "none",
            field="cancellation_policy",
        )
        statement = policy_statement(facts)
        payload["message"] = statement["message"]
        payload["status"] = statement["status"]
        payload["policies"] = statement
        return payload

    if topic in {"packages", "vip"}:
        truth("SPA_DETAIL_LOOKUP", source="dashboard" if facts["packages"] else "none", field=topic)
        if topic == "vip":
            vip = configured_vip(facts["packages"], vip_identifier)
            payload["vip"] = vip
            payload["message"] = vip["message"]
            payload["status"] = vip["status"]
            return payload
        payload["message"] = facts["packages"] or UNKNOWN
        payload["status"] = "ok" if facts["packages"] else "unknown"
        return payload

    if topic in {"upsells", "upsell"}:
        truth("SPA_DETAIL_LOOKUP", source="dashboard" if upsells else "none", field="upsells")
        if not upsells:
            payload["status"] = "unknown"
            payload["message"] = (
                "No configured upsell exists for this service. Do not invent packages, "
                "add-ons, upgrades, discounts, or VIP programs."
            )
        else:
            payload["message"] = "Configured upsells: " + "; ".join(upsells)
        return payload

    if topic in {"payment", "card"}:
        truth("SPA_DETAIL_LOOKUP", source="dashboard", field="payment")
        if payment["card_required"] and payment["collection_mode"] == "none":
            payload["message"] = (
                "A card may be required by policy, but no phone collection flow is "
                "enabled. Do not ask the caller to read a card number. "
                + str(payment.get("script") or DEFAULT_PAYMENT_POLICY["script"])
            )
        elif not payment["card_required"]:
            payload["message"] = (
                "No card is required to complete this booking over the phone. "
                "Do not collect or store card numbers."
            )
        elif payment["collection_mode"] == "at_spa":
            payload["message"] = (
                "A card is required at the spa, not over the phone. Do not collect PAN or CVV."
            )
        elif payment["collection_mode"] == "square_link":
            payload["message"] = (
                "If a card is needed, Square sends a secure link. Do not collect the number by voice."
            )
        elif payment["collection_mode"] == "secure_sms_link":
            from app.services.booking_state import CARD_ON_FILE_POLICY, CARD_ON_FILE_HESITANT
            payload["booking_policy"] = policy_statement(facts)
            payload["message"] = (
                "A card is required on file. Before booking, say exactly: "
                + CARD_ON_FILE_POLICY + " "
                "If the caller hesitates, say exactly: "
                + CARD_ON_FILE_HESITANT + " "
                "Do not paraphrase either sentence. After booking, mention a secure text only if "
                "the backend says the text was sent. Do not ask the caller to speak a card number. "
                "Quote cancellation windows or fees ONLY from this business's configured booking_policy."
            )
        elif payment["collection_mode"] == "secure_voice_card":
            from app.services.secure_payment import secure_collection_plan

            plan = secure_collection_plan(payment)
            payload["payment_handoff"] = plan
            payload["message"] = plan["message"]
            payload["status"] = "ok" if plan["status"] == "handoff" else "unavailable"
        else:
            payload["message"] = str(payment.get("script") or DEFAULT_PAYMENT_POLICY["script"])
        return payload

    payload["message"] = (
        f"Name: {facts['spa_name']}. "
        f"Address: {address or UNKNOWN}. "
        f"Phone: {payload['phone'] or UNKNOWN}."
    )
    return payload
