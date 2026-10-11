"""Opt-in, tenant-scoped customer memory for Cara's inbound spa calls.

Caller ID is only a lookup hint. Names and preferences are disclosed only after
a caller-provided full name matches the same spa's local/Square customer.
The memory is deliberately limited to non-sensitive, explicitly requested
preferences. Booking history remains in existing verified appointment records.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import scope_filter, scope_columns
from app.models import Appointment, AppointmentStatus, Contact, SpaAccount
from app.services.booking_adapters.customers import CustomerLookupOutcome
from app.services.booking_adapters.spa_router import SpaBookingAdapter
from app.services.call_state import CallSession
from app.services.phone_numbers import canonical_customer_phone, is_non_phone_caller_id

MEMORY_KEY = "cara_preferences_v1"
PREFERENCE_KEYS = frozenset({
    "favorite_service", "preferred_staff", "preferred_time", "treatment_style"
})
_REMEMBER_INTENT = re.compile(
    r"\b(?:remember|save (?:that|this|my)|keep (?:that|this|my) on file|"
    r"please remember)\b", re.I
)
_FORGET_INTENT = re.compile(
    r"\b(?:forget|delete|remove|don't remember|do not remember)\b", re.I
)
# No health/medical notes, payment details, personal identifiers or secrets in
# receptionist personalization. Such details belong in dedicated provider
# workflows, never a persistent convenience-profile field.
_PRIVATE = re.compile(
    r"\b(?:allerg(?:y|ies|ic)|asthma|pregnan\w*|diabet\w*|cancer|"
    r"acne|breakouts?|medicat\w*|prescript\w*|diagnos\w*|"
    r"injur\w*|pain|trauma|surgery|infection|rash|"
    r"card|credit|debit|cvv|cvc|pin|password|ssn|"
    r"social security|account number|bank|address|birth(?:day|date))\b",
    re.I,
)
_NAME_PREFIX = re.compile(
    r"\b(?:my name is|this is|i am|i'm|it's|it is|"
    r"name on (?:my |the )?(?:account|booking) is)\s+",
    re.I,
)


def _norm(value: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _full_name_match(on_file: str | None, claimed: str | None) -> bool:
    """A shared phone alone does not establish the person's identity."""
    a, b = _norm(on_file), _norm(claimed)
    return bool(a and b and len(a.split()) >= 2 and a == b)


def _last_caller_turn(session: CallSession) -> str:
    for turn in reversed(session.history):
        if turn.get("role") == "user":
            return str(turn.get("content") or "")
    return ""


def _claimed_name_grounded(session: CallSession, claimed_name: str) -> bool:
    """Do not trust an LLM-invented name in tool arguments."""
    utterance = _norm(_last_caller_turn(session))
    name = _norm(claimed_name)
    if not utterance or not name or len(name.split()) < 2:
        return False
    if utterance == name:
        return True
    raw = _last_caller_turn(session)
    for prefix in _NAME_PREFIX.finditer(raw):
        if _norm(raw[prefix.end():]).startswith(name):
            return True
    return False


def _safe_preference(key: str, value: str, utterance: str) -> str | None:
    """Store only caller-spoken, non-sensitive short preferences."""
    if key not in PREFERENCE_KEYS or not isinstance(value, str):
        return None
    clean = " ".join(value.strip().split())
    if not clean or len(clean) > 100 or "\n" in value:
        return None
    if _PRIVATE.search(clean) or _PRIVATE.search(utterance):
        return None
    if re.search(r"(?:\d[ -]?){7,}|@", clean):
        return None
    # A model may not manufacture a preference absent from caller speech.
    if _norm(clean) not in _norm(utterance):
        return None
    return clean


def _saved_preferences(contact: Contact | None) -> dict[str, str]:
    meta = getattr(contact, "extra_metadata", None) or {}
    stored = meta.get(MEMORY_KEY) if isinstance(meta, dict) else None
    prefs = stored.get("preferences", {}) if isinstance(stored, dict) else {}
    if not isinstance(prefs, dict):
        return {}
    return {
        k: v for k, v in prefs.items()
        if k in PREFERENCE_KEYS and isinstance(v, str)
        and len(v) <= 100 and not _PRIVATE.search(v)
    }


def _update_preferences(contact: Contact, preferences: dict[str, str]) -> None:
    # Assign a fresh dict so SQLAlchemy tracks JSONB changes reliably.
    meta = dict(contact.extra_metadata or {})
    store = dict(meta.get(MEMORY_KEY) or {})
    store["preferences"] = dict(preferences)
    store["updated_at"] = datetime.now(timezone.utc).isoformat()
    meta[MEMORY_KEY] = store
    contact.extra_metadata = meta


async def _resolve_profile(
    db: AsyncSession, session: CallSession, caller_name: str,
) -> tuple[Contact | None, str | None, str]:
    """Look up local contact, then a Square profile by *this spa* and phone.

    Return (local_contact, confirmed_profile_name, status). Do not modify any
    customer or search other spas. If two people share a number, a full name
    must identify the correct provider profile.
    """
    scope = session.scope
    if (
        session.direction != "inbound"
        or scope is None
        or scope.tenant_id is None
        or is_non_phone_caller_id(session.customer_phone)
    ):
        return None, None, "unsupported"
    phone = canonical_customer_phone(session.customer_phone, timezone_name=session.timezone)
    if not phone:
        return None, None, "unsupported"

    contact = (
        await db.execute(
            select(Contact).where(
                scope_filter(scope, Contact), Contact.phone_number == phone,
            )
        )
    ).scalar_one_or_none()
    if contact is not None and _full_name_match(contact.full_name, caller_name):
        return contact, contact.full_name, "verified"

    # The spa may have a client in Square who has never used Cara. Search the
    # connected provider only, and fail closed on timeouts or ambiguous names.
    spa = (
        await db.execute(select(SpaAccount).where(SpaAccount.id == scope.tenant_id))
    ).scalar_one_or_none()
    if spa is None:
        return None, None, "not_found"
    try:
        adapter = SpaBookingAdapter(spa)
        if not adapter.supports_customer_lookup:
            return None, None, "not_found"
        result = await asyncio.wait_for(
            adapter.find_customers_by_phone(phone), timeout=4.0
        )
    except Exception:
        return None, None, "lookup_unavailable"
    if result.outcome == CustomerLookupOutcome.SEARCH_FAILED:
        return None, None, "lookup_unavailable"
    if result.outcome not in {
        CustomerLookupOutcome.MATCHED, CustomerLookupOutcome.AMBIGUOUS
    }:
        return None, None, "not_found"
    matches = [
        item for item in result.candidates
        if _full_name_match(item.display_name, caller_name)
    ]
    if len(matches) != 1:
        return None, None, "not_found"
    # The same number can belong to more than one person. A local CRM row for
    # somebody else is never assigned to the Square-matched person.
    return None, matches[0].display_name, "verified_square"


async def _recent_treatments(
    db: AsyncSession, session: CallSession, contact: Contact | None
) -> list[str]:
    if contact is None or session.scope is None:
        return []
    result = await db.execute(
        select(Appointment)
        .where(
            scope_filter(session.scope, Appointment),
            Appointment.contact_id == contact.id,
            Appointment.status.in_([
                AppointmentStatus.SCHEDULED,
                AppointmentStatus.CONFIRMED,
                AppointmentStatus.COMPLETED,
            ]),
            Appointment.start_time < datetime.now(timezone.utc),
        )
        .order_by(Appointment.start_time.desc())
        .limit(3)
    )
    names = []
    for appt in result.scalars().all():
        name = str(getattr(appt, "title", "") or "").strip()
        if name and name not in names:
            names.append(name)
    return names



def _safe_feedback(message: str, utterance: str) -> str | None:
    """A deliberately short, non-medical comment volunteered by the caller."""
    if not isinstance(message, str):
        return None
    clean = " ".join(message.strip().split())
    if not 3 <= len(clean) <= 240:
        return None
    if _PRIVATE.search(clean) or re.search(r"(?:\d[ -]?){7,}|@", clean):
        return None
    if _norm(clean) not in _norm(utterance):
        return None
    return clean


async def _latest_previous_visit(
    db: AsyncSession, session: CallSession, contact: Contact | None
) -> dict[str, str] | None:
    if contact is None or session.scope is None:
        return None
    result = await db.execute(
        select(Appointment)
        .where(
            scope_filter(session.scope, Appointment),
            Appointment.contact_id == contact.id,
            Appointment.status.in_([
                AppointmentStatus.SCHEDULED,
                AppointmentStatus.CONFIRMED,
                AppointmentStatus.COMPLETED,
            ]),
            Appointment.start_time < datetime.now(timezone.utc),
        )
        .order_by(Appointment.start_time.desc())
        .limit(1)
    )
    appt = result.scalars().first()
    if appt is None:
        return None
    return {
        "appointment_id": str(appt.id),
        "service": str(appt.title or ""),
        "date": appt.start_time.date().isoformat(),
    }


async def _local_contact_for_write(
    db: AsyncSession, session: CallSession, contact: Contact | None,
    on_file_name: str
) -> Contact | None:
    if contact is not None:
        return contact
    scope = session.scope
    if scope is None or scope.tenant_id is None:
        return None
    phone = canonical_customer_phone(
        session.customer_phone, timezone_name=session.timezone
    )
    existing = (
        await db.execute(
            select(Contact).where(
                scope_filter(scope, Contact), Contact.phone_number == phone
            )
        )
    ).scalar_one_or_none()
    # One local CRM row per tenant/phone. Do not silently overwrite a shared
    # number belonging to a different person.
    if existing is not None:
        return existing if _full_name_match(existing.full_name, on_file_name) else None
    pieces = on_file_name.split(maxsplit=1)
    if len(pieces) < 2:
        return None
    created = Contact(
        phone_number=phone,
        first_name=pieces[0],
        last_name=pieces[1],
        **scope_columns(scope, Contact),
    )
    db.add(created)
    await db.flush()
    return created


def _append_visit_feedback(
    contact: Contact, feedback: str, last_visit: dict[str, str] | None
) -> None:
    meta = dict(contact.extra_metadata or {})
    store = dict(meta.get(MEMORY_KEY) or {})
    entries = list(store.get("visit_feedback") or [])
    entries.append({
        "feedback": feedback,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "appointment_id": last_visit["appointment_id"] if last_visit else None,
        "verified_visit": bool(last_visit),
        "service": last_visit["service"] if last_visit else None,
        "visit_date": last_visit["date"] if last_visit else None,
    })
    store["visit_feedback"] = entries[-10:]
    store["updated_at"] = datetime.now(timezone.utc).isoformat()
    meta[MEMORY_KEY] = store
    contact.extra_metadata = meta


async def customer_profile_action(
    db: AsyncSession,
    session: CallSession,
    *,
    action: str = "lookup",
    caller_name: str | None = None,
    preference_key: str | None = None,
    preference_value: str | None = None,
    feedback_text: str | None = None,
) -> dict[str, Any]:
    """Lookup, remember, or forget preferences without trusting model claims.

    No booking is made, no Square profile is modified, no raw call transcript is
    copied into CRM memory. All writes are to an identity-verified contact
    within the current spa.
    """
    if action not in {"lookup", "remember", "forget", "feedback_invite", "feedback_save"}:
        return {"status": "invalid_action", "message": "Please try again."}
    if session.direction != "inbound" or not session.tenant_id:
        return {"status": "unsupported", "message": "This customer lookup is unavailable."}

    verified = session.entities.get("verified_customer_profile")
    if isinstance(verified, dict) and verified.get("name"):
        if caller_name and not _full_name_match(verified["name"], caller_name):
            # Never switch identities by substituting tool arguments.
            return {"status": "name_mismatch", "message": "Please clarify whose profile you mean."}
        name = verified["name"]
    else:
        name = " ".join((caller_name or "").split())
        if not _claimed_name_grounded(session, name):
            session.entities["awaiting_customer_profile_name"] = True
            return {
                "status": "name_required",
                "message": "I'd be happy to check. What's the full name on your account?",
            }

    contact, on_file_name, outcome = await _resolve_profile(db, session, name)
    if not on_file_name:
        if outcome == "lookup_unavailable":
            return {
                "status": "lookup_unavailable",
                "message": "I'm having trouble looking up profiles right now. We can still book an appointment.",
            }
        return {
            "status": "not_found",
            "message": "I couldn't verify a profile under that name and number. We can still help with an appointment.",
        }
    session.entities["verified_customer_profile"] = {"name": on_file_name}
    session.entities.pop("awaiting_customer_profile_name", None)

    if action == "feedback_invite":
        # Asking does not save anything and does not impede a new booking.
        if session.entities.get("feedback_invited_this_call"):
            return {
                "status": "already_invited",
                "message": "Continue with the caller's current request.",
            }
        visit = await _latest_previous_visit(db, session, contact)
        count = sum(turn.get("role") == "user" for turn in session.history)
        session.entities["awaiting_visit_feedback"] = {
            "invited_at_turn": count,
            "last_visit": visit,
        }
        session.entities["feedback_invited_this_call"] = True
        if visit:
            return {
                "status": "feedback_invited",
                "message": "If you have a moment, how was your last visit with us?",
                "last_verified_service": visit["service"],
                "last_verified_date": visit["date"],
            }
        return {
            "status": "feedback_invited",
            "message": "If you've visited us before, would you like to share how your last experience was?",
        }

    if action == "feedback_save":
        pending = session.entities.get("awaiting_visit_feedback")
        if not isinstance(pending, dict):
            return {
                "status": "not_requested",
                "message": "I can ask for feedback after helping with your appointment.",
            }
        count = sum(turn.get("role") == "user" for turn in session.history)
        if count <= pending.get("invited_at_turn", count):
            return {"status": "awaiting_answer", "message": "Wait for the caller's reply."}
        utterance = _last_caller_turn(session)
        session.entities.pop("awaiting_visit_feedback", None)
        if re.search(r"\b(?:no thanks|not now|no thank you|skip|rather not)\b", utterance, re.I):
            return {"status": "declined", "message": "Of course! Is there anything else I can help with?"}
        clean = _safe_feedback(feedback_text or "", utterance)
        if clean is None:
            return {
                "status": "not_saved",
                "message": "Thank you for sharing. I couldn't save those details to the general profile.",
            }
        local_contact = await _local_contact_for_write(db, session, contact, on_file_name)
        if local_contact is None:
            return {
                "status": "shared_number",
                "message": "I can't safely attach feedback to that shared-number profile. Staff can help with it.",
            }
        _append_visit_feedback(local_contact, clean, pending.get("last_visit"))
        await db.commit()
        return {
            "status": "feedback_saved",
            "message": "Thank you for the feedback! I'll keep that on file for the spa team.",
        }

    if action == "remember":
        utterance = _last_caller_turn(session)
        if not _REMEMBER_INTENT.search(utterance):
            return {
                "status": "consent_required",
                "message": "I can save that preference if you'd like. Please tell me to remember it.",
            }
        clean = _safe_preference(preference_key or "", preference_value or "", utterance)
        if clean is None:
            return {
                "status": "unsupported_preference",
                "message": "I can remember treatment, staff, time, and non-medical style preferences, but not sensitive personal details.",
            }
        contact = await _local_contact_for_write(db, session, contact, on_file_name)
        if contact is None:
            return {
                "status": "shared_number",
                "message": "That phone number has an identity conflict. Staff can help save your preference safely.",
            }
        prefs = _saved_preferences(contact)
        prefs[preference_key] = clean
        _update_preferences(contact, prefs)
        await db.commit()
        return {"status": "saved", "message": "I'll remember that preference for your future visits."}

    if action == "forget":
        utterance = _last_caller_turn(session)
        if not _FORGET_INTENT.search(utterance):
            return {
                "status": "consent_required",
                "message": "Please tell me which saved preference to forget, or ask me to forget all preferences.",
            }
        if contact is None:
            return {"status": "forgotten", "message": "There were no saved Cara preferences on that profile."}
        prefs = _saved_preferences(contact)
        if preference_key == "all":
            prefs.clear()
            _update_preferences(contact, prefs)
            meta = dict(contact.extra_metadata or {})
            store = dict(meta.get(MEMORY_KEY) or {})
            store.pop("visit_feedback", None)
            meta[MEMORY_KEY] = store
            contact.extra_metadata = meta
            await db.commit()
            return {"status": "forgotten", "message": "I've cleared your saved preferences and feedback."}
        elif preference_key == "feedback":
            meta = dict(contact.extra_metadata or {})
            store = dict(meta.get(MEMORY_KEY) or {})
            store.pop("visit_feedback", None)
            meta[MEMORY_KEY] = store
            contact.extra_metadata = meta
            await db.commit()
            return {"status": "forgotten", "message": "I've cleared your saved feedback."}
        elif preference_key in PREFERENCE_KEYS:
            prefs.pop(preference_key, None)
        else:
            return {
                "status": "clarify_preference",
                "message": "Which preference would you like me to forget, or should I forget them all?",
            }
        _update_preferences(contact, prefs)
        await db.commit()
        return {"status": "forgotten", "message": "I've cleared that saved preference."}

    return {
        "status": "found",
        "recognized": True,
        "name": on_file_name,
        "source": "existing_spa_customer",
        "preferences": _saved_preferences(contact),
        "recent_verified_services": await _recent_treatments(db, session, contact),
        "message": (
            "Welcome back! These are verified local booking records and saved preferences. "
            "Do not assume favorite services are still on the menu or available; "
            "check current availability before proposing or booking."
        ),
    }
