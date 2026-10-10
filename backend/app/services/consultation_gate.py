"""Pure, server-owned state transitions for spa service consultations.

The realtime model can phrase a transition returned by this module, but it must
not decide whether the consultation is complete.  Every helper accepts and
returns JSON-safe dictionaries so the voice layer can keep the state in the
existing call-session entity bag.

Free-form consultation answers are used only during the current function call.
They are reduced to an answered-field marker and a broad cosmetic/wellness
category.  In particular, injury and area-to-avoid text is never copied into
the returned state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.services.service_consultation import (
    classify_facial_need,
    classify_massage_need,
    configured_duration_choices,
    relevant_configured_addons,
)


FACIAL = "facial"
MASSAGE = "massage"
CONSULTATION_KINDS = frozenset({FACIAL, MASSAGE})

REQUIRED_QUESTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    FACIAL: (
        (
            "main_concern",
            "What's the main thing bothering you about your skin right now?",
        ),
        (
            "skin_feel",
            "How does your skin usually feel by midday or the end of the day—oily, dry, combination, or balanced?",
        ),
        (
            "skin_flags",
            "Do you have any breakouts, sensitivity, or redness?",
        ),
    ),
    MASSAGE: (
        (
            "massage_reason",
            "What's the main reason you're wanting a massage today?",
        ),
        (
            "massage_areas",
            "Are there any particular areas bothering you most?",
        ),
        (
            "pressure_preference",
            "Do you prefer lighter, medium, or deeper pressure?",
        ),
        (
            "safety_answered",
            "Do you have any injuries or areas we should avoid?",
        ),
    ),
}

_DURATION_RE = re.compile(
    r"\b(?P<minutes>30|60|thirty|sixty)\s*(?:-|\s)*(?:minute|minutes|min|mins)\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

_FACIAL_CATEGORY_PRIORITY = {
    "custom": 1,
    "hydrating": 2,
    "acne_clarifying": 3,
    "calming_barrier_repair": 4,
}
_MASSAGE_CATEGORY_PRIORITY = {
    "customized": 1,
    "relaxation": 2,
    "focused_therapeutic": 3,
    "deeper_pressure_sports": 4,
}
_MASSAGE_ADDON_SIGNALS = {
    "relaxation": "relaxation",
    "focused_therapeutic": "muscle tension",
    "deeper_pressure_sports": "deep pressure recovery",
}


def _normalized(value: object) -> str:
    return " ".join(_TOKEN_RE.findall(str(value or "").casefold()))


def _service_name(service: Mapping[str, Any]) -> str:
    return str(service.get("name") or "").strip()


def _duration_minutes(service: Mapping[str, Any] | str | None) -> int | None:
    if isinstance(service, Mapping):
        value = service.get("duration_minutes")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        text = _service_name(service)
    else:
        text = str(service or "")
    match = _DURATION_RE.search(text)
    if not match:
        return None
    return 30 if match.group("minutes").casefold() in {"30", "thirty"} else 60


def _service_kind(service: Mapping[str, Any] | str | None) -> str | None:
    if isinstance(service, Mapping):
        explicit = _normalized(
            service.get("consultation_kind") or service.get("service_kind")
        )
        if explicit in CONSULTATION_KINDS:
            return explicit
        parts = (
            _service_name(service),
            service.get("category"),
            service.get("service_family"),
        )
    else:
        parts = (service,)
    words = set(_normalized(" ".join(str(part or "") for part in parts)).split())
    has_facial = "facial" in words or "facials" in words
    has_massage = "massage" in words or "massages" in words
    if has_facial == has_massage:
        return None
    return FACIAL if has_facial else MASSAGE


def infer_consultation_kind(
    requested_service: Mapping[str, Any] | str | None,
    services: Sequence[Mapping[str, Any]] = (),
) -> str | None:
    """Infer facial/massage from explicit metadata, a catalog match, or its name."""

    direct = _service_kind(requested_service)
    if direct:
        return direct
    wanted = _normalized(
        _service_name(requested_service)
        if isinstance(requested_service, Mapping)
        else requested_service
    )
    if not wanted:
        return None
    catalog_matches = [
        service
        for service in services
        if _normalized(_service_name(service)) == wanted
    ]
    kinds = {_service_kind(service) for service in catalog_matches}
    kinds.discard(None)
    return kinds.pop() if len(kinds) == 1 else None


def _clean_string_list(value: object, *, allowed: set[str] | None = None) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text or (allowed is not None and text not in allowed) or text in result:
            continue
        result.append(text)
    return result


def _safe_prior_state(prior_state: Mapping[str, Any] | None, kind: str) -> dict[str, Any]:
    """Copy only the state fields this module intentionally persists."""

    prior = prior_state if isinstance(prior_state, Mapping) else {}
    required = {field for field, _ in REQUIRED_QUESTIONS[kind]}
    safe: dict[str, Any] = {
        "kind": kind,
        "answered_fields": _clean_string_list(
            prior.get("answered_fields"), allowed=required
        ),
        "asked_fields": _clean_string_list(prior.get("asked_fields"), allowed=required),
        "category": (
            str(prior.get("category") or "").strip() or None
            if prior.get("kind") == kind
            else None
        ),
        "duration_offer_presented": bool(prior.get("duration_offer_presented")),
        "addon_offer_presented": bool(prior.get("addon_offer_presented")),
        "requested_service_name": str(
            prior.get("requested_service_name") or ""
        ).strip()
        or None,
        "requested_duration_minutes": _positive_int(
            prior.get("requested_duration_minutes")
        ),
        "selected_service_name": str(
            prior.get("selected_service_name") or ""
        ).strip()
        or None,
        "selected_duration_minutes": _positive_int(
            prior.get("selected_duration_minutes")
        ),
        "duration_choices": _safe_duration_choices(prior.get("duration_choices")),
        "offered_addon_names": _clean_string_list(
            prior.get("offered_addon_names")
        )[:2],
    }
    return safe


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _safe_duration_choices(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        minutes = _positive_int(item.get("minutes"))
        name = str(item.get("service_name") or "").strip()
        if minutes not in {30, 60} or not name or minutes in seen:
            continue
        result.append({"minutes": minutes, "service_name": name})
        seen.add(minutes)
    return result


def initialize_consultation(
    requested_service: Mapping[str, Any] | str,
    *,
    services: Sequence[Mapping[str, Any]] = (),
    requested_duration_minutes: int | None = None,
    prior_state: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Start or resume a consultation without losing an explicit duration.

    Omitting a duration on a later model/tool call does not clear a duration the
    caller already requested.  Passing another positive duration updates it.
    Changing modality starts a fresh consultation.
    """

    kind = infer_consultation_kind(requested_service, services)
    if kind not in CONSULTATION_KINDS:
        return None
    same_kind = isinstance(prior_state, Mapping) and prior_state.get("kind") == kind
    state = _safe_prior_state(prior_state if same_kind else None, kind)
    if isinstance(requested_service, Mapping):
        requested_name = _service_name(requested_service)
    else:
        requested_name = str(requested_service or "").strip()
    previous_name = state.get("requested_service_name")
    if requested_name:
        state["requested_service_name"] = requested_name

    explicit_duration = _positive_int(requested_duration_minutes)
    if explicit_duration is None:
        explicit_duration = _duration_minutes(requested_service)
    if explicit_duration is not None:
        state["requested_duration_minutes"] = explicit_duration
        state["selected_duration_minutes"] = explicit_duration

    if same_kind and requested_name and previous_name and (
        _normalized(requested_name) != _normalized(previous_name)
    ):
        # Consultation answers still describe the caller, but any menu offers
        # were grounded against the previous service and must be rebuilt.
        state["duration_offer_presented"] = False
        state["addon_offer_presented"] = False
        state["duration_choices"] = []
        state["offered_addon_names"] = []
        state["selected_service_name"] = None
    return state


def _merge_category(kind: str, old: str | None, new: str | None) -> str | None:
    if not new:
        return old
    priorities = (
        _FACIAL_CATEGORY_PRIORITY if kind == FACIAL else _MASSAGE_CATEGORY_PRIORITY
    )
    if not old or priorities.get(new, 0) >= priorities.get(old, 0):
        return new
    return old


def record_consultation_answer(
    state: Mapping[str, Any], *, field: str, answer: object = None
) -> dict[str, Any]:
    """Record safe progress and discard the caller's raw answer.

    For ``safety_answered``, even category inference is skipped.  The returned
    dictionary therefore cannot contain injury or area-to-avoid wording.
    """

    kind = str(state.get("kind") or "")
    if kind not in CONSULTATION_KINDS:
        raise ValueError("consultation state has no supported kind")
    result = _safe_prior_state(state, kind)
    required = [name for name, _ in REQUIRED_QUESTIONS[kind]]
    if field not in required:
        raise ValueError(f"{field!r} is not a {kind} consultation field")

    answered = result["answered_fields"]
    if field not in answered:
        answered.append(field)
    raw = str(answer or "").strip()
    category: str | None = None
    if raw and field != "safety_answered":
        if kind == FACIAL:
            category = classify_facial_need(raw)
        else:
            category = classify_massage_need(raw)
    result["category"] = _merge_category(kind, result.get("category"), category)
    return result


def record_pending_answer(
    state: Mapping[str, Any], *, caller_utterance: str
) -> dict[str, Any]:
    """Apply the latest caller turn to the one pending server-owned question.

    The pending field comes from state, not from a model-supplied tool argument.
    This prevents a model from skipping questions by claiming fields were
    answered.  The utterance is consumed transiently for broad classification
    and is absent from the returned JSON-safe state.
    """

    kind = str(state.get("kind") or "")
    if kind not in CONSULTATION_KINDS:
        raise ValueError("consultation state has no supported kind")
    result = _safe_prior_state(state, kind)
    utterance = str(caller_utterance or "").strip()
    if not _normalized(utterance):
        return {
            "state": result,
            "status": "no_answer",
            "answered_field": None,
        }
    answered = set(result["answered_fields"])
    asked = set(result["asked_fields"])
    pending = next(
        (
            field
            for field, _ in REQUIRED_QUESTIONS[kind]
            if field in asked and field not in answered
        ),
        None,
    )
    if pending is None:
        return {
            "state": result,
            "status": "no_pending_question",
            "answered_field": None,
        }
    result = record_consultation_answer(
        result,
        field=pending,
        answer=utterance,
    )
    return {
        "state": result,
        "status": "answer_recorded",
        "answered_field": pending,
    }


def take_next_question(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return at most one not-yet-asked required question.

    If the outstanding question was already asked, ``awaiting_answer`` is
    returned without repeating its wording.
    """

    kind = str(state.get("kind") or "")
    if kind not in CONSULTATION_KINDS:
        raise ValueError("consultation state has no supported kind")
    result = _safe_prior_state(state, kind)
    answered = set(result["answered_fields"])
    asked = result["asked_fields"]
    for field, question in REQUIRED_QUESTIONS[kind]:
        if field in answered:
            continue
        if field in asked:
            return {
                "state": result,
                "status": "awaiting_answer",
                "question": None,
                "awaiting_field": field,
            }
        asked.append(field)
        return {
            "state": result,
            "status": "ask_question",
            "question": {"field": field, "text": question},
            "awaiting_field": field,
        }
    return {
        "state": result,
        "status": "complete",
        "question": None,
        "awaiting_field": None,
    }


def consultation_complete(state: Mapping[str, Any]) -> bool:
    kind = str(state.get("kind") or "")
    if kind not in CONSULTATION_KINDS:
        return False
    answered = set(_clean_string_list(state.get("answered_fields")))
    return all(field in answered for field, _ in REQUIRED_QUESTIONS[kind])


def _catalog_service(
    services: Sequence[Mapping[str, Any]], name: object
) -> Mapping[str, Any] | None:
    wanted = _normalized(name)
    if not wanted:
        return None
    exact = [service for service in services if _normalized(_service_name(service)) == wanted]
    return exact[0] if len(exact) == 1 else None


def _duration_options(
    state: Mapping[str, Any],
    services: Sequence[Mapping[str, Any]],
    recommended_service: Mapping[str, Any] | str | None,
) -> list[dict[str, Any]]:
    kind = str(state.get("kind") or "")
    anchors: list[Mapping[str, Any] | str] = []
    requested_name = state.get("requested_service_name")
    if requested_name:
        anchors.append(str(requested_name))
    if recommended_service:
        anchors.append(recommended_service)

    choices_by_duration: dict[int, dict[str, Any]] = {}
    for anchor in anchors:
        anchored = configured_duration_choices(services, anchor)
        for choice in anchored.get("choices") or []:
            minutes = _positive_int(choice.get("minutes"))
            service = choice.get("service")
            if minutes in {30, 60} and isinstance(service, Mapping):
                choices_by_duration.setdefault(
                    minutes,
                    {"minutes": minutes, "service": dict(service)},
                )

    # Prefer the same menu family, then fill a missing duration using another
    # real service of the same modality.  Catalog order is the deterministic
    # tiebreaker; actual service names remain visible in the structured result.
    for service in services:
        if service.get("is_add_on") or _service_kind(service) != kind:
            continue
        minutes = _duration_minutes(service)
        if minutes in {30, 60}:
            choices_by_duration.setdefault(
                minutes,
                {"minutes": minutes, "service": dict(service)},
            )
    return [choices_by_duration[m] for m in (30, 60) if m in choices_by_duration]


def _duration_offer_text(kind: str, choices: Sequence[Mapping[str, Any]]) -> str:
    by_minutes = {int(choice["minutes"]): choice for choice in choices}
    parts: list[str] = []
    if 30 in by_minutes:
        name = _service_name(by_minutes[30]["service"])
        benefit = (
            "is a focused start"
            if kind == FACIAL
            else "can focus well on specific areas"
        )
        parts.append(f"The 30-minute {name} {benefit}.")
    if 60 in by_minutes:
        name = _service_name(by_minutes[60]["service"])
        benefit = (
            "gives us more time to target your concern so the results can last longer"
            if kind == FACIAL
            else "gives the therapist time to work more thoroughly for longer-lasting relief"
        )
        parts.append(f"The 60-minute {name} {benefit}.")
    if len(by_minutes) == 2:
        parts.append("Would you prefer the 60-minute or the 30-minute?")
    elif by_minutes:
        only = next(iter(by_minutes))
        parts.append(f"Would you like the {only}-minute option?")
    return " ".join(parts)


def take_duration_offer(
    state: Mapping[str, Any],
    services: Sequence[Mapping[str, Any]],
    *,
    recommended_service: Mapping[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Ground and return the one permitted duration offer for this consultation."""

    kind = str(state.get("kind") or "")
    if kind not in CONSULTATION_KINDS:
        raise ValueError("consultation state has no supported kind")
    result = _safe_prior_state(state, kind)
    if not consultation_complete(result):
        return {"state": result, "status": "consultation_incomplete", "offer": None}
    if result["duration_offer_presented"]:
        return {"state": result, "status": "already_presented", "offer": None}

    options = _duration_options(result, services, recommended_service)
    if not options:
        return {"state": result, "status": "no_catalog_options", "offer": None}
    result["duration_offer_presented"] = True
    result["duration_choices"] = [
        {
            "minutes": choice["minutes"],
            "service_name": _service_name(choice["service"]),
        }
        for choice in options
    ]
    requested = result.get("requested_duration_minutes")
    requested_choice = next(
        (choice for choice in result["duration_choices"] if choice["minutes"] == requested),
        None,
    )
    if requested_choice:
        # Presenting both options is advisory.  It must not erase a duration the
        # caller already supplied unless a later caller turn explicitly changes it.
        result["selected_duration_minutes"] = requested_choice["minutes"]
        result["selected_service_name"] = requested_choice["service_name"]
    offer = {
        "kind": kind,
        "choices": result["duration_choices"],
        "requested_duration_minutes": result.get("requested_duration_minutes"),
        "text": _duration_offer_text(kind, options),
    }
    return {"state": result, "status": "offer_duration", "offer": offer}


def record_duration_selection(
    state: Mapping[str, Any],
    *,
    selected_duration_minutes: int | None,
    selected_service_name: str | None = None,
) -> dict[str, Any]:
    """Record an explicit caller change; omission preserves the prior duration."""

    kind = str(state.get("kind") or "")
    if kind not in CONSULTATION_KINDS:
        raise ValueError("consultation state has no supported kind")
    result = _safe_prior_state(state, kind)
    minutes = _positive_int(selected_duration_minutes)
    if minutes is None:
        return result
    choices = result.get("duration_choices") or []
    matching = [choice for choice in choices if choice.get("minutes") == minutes]
    if choices and not matching:
        raise ValueError("selected duration was not in the grounded catalog offer")
    result["requested_duration_minutes"] = minutes
    result["selected_duration_minutes"] = minutes
    if selected_service_name:
        if matching and not any(
            _normalized(choice.get("service_name")) == _normalized(selected_service_name)
            for choice in matching
        ):
            raise ValueError("selected service was not in the grounded catalog offer")
        result["selected_service_name"] = str(selected_service_name).strip()
    elif matching:
        result["selected_service_name"] = matching[0]["service_name"]
    # A caller changed the duration, so any prior add-on offer was tied to a
    # different variation and may be recomputed once.
    result["addon_offer_presented"] = False
    result["offered_addon_names"] = []
    return result


def take_massage_addon_offer(
    state: Mapping[str, Any],
    services: Sequence[Mapping[str, Any]],
    upsell_rules: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return at most two configured relevant add-ons after duration selection."""

    kind = str(state.get("kind") or "")
    if kind not in CONSULTATION_KINDS:
        raise ValueError("consultation state has no supported kind")
    result = _safe_prior_state(state, kind)
    if kind != MASSAGE:
        return {"state": result, "status": "not_applicable", "offer": None}
    if not consultation_complete(result):
        return {"state": result, "status": "consultation_incomplete", "offer": None}
    if result["addon_offer_presented"]:
        return {"state": result, "status": "already_presented", "offer": None}
    selected_name = result.get("selected_service_name")
    selected_minutes = result.get("selected_duration_minutes")
    selected = _catalog_service(services, selected_name)
    if not selected or _duration_minutes(selected) != selected_minutes:
        return {"state": result, "status": "duration_not_selected", "offer": None}

    signal = _MASSAGE_ADDON_SIGNALS.get(str(result.get("category") or ""), "")
    addons = relevant_configured_addons(
        services,
        upsell_rules,
        base_service=selected,
        stated_needs=(signal,),
        limit=2,
    )
    result["addon_offer_presented"] = True
    descriptions = [
        {
            "service_name": _service_name(item["service"]),
            "description": str(item.get("benefit") or "").strip(),
        }
        for item in addons[:2]
    ]
    result["offered_addon_names"] = [
        item["service_name"] for item in descriptions
    ]
    status = "offer_addons" if descriptions else "no_relevant_addons"
    return {
        "state": result,
        "status": status,
        "offer": {"items": descriptions} if descriptions else None,
    }


def availability_gate(state: Mapping[str, Any]) -> dict[str, Any]:
    """Decide whether the voice layer may contact the availability provider.

    Call this immediately before every availability lookup and appointment
    proposal.  It intentionally has no model dependency.
    """

    kind = str(state.get("kind") or "")
    if kind not in CONSULTATION_KINDS:
        return {"allow": True, "status": "not_applicable"}
    result = _safe_prior_state(state, kind)
    if not consultation_complete(result):
        return {"allow": False, "status": "consultation_required"}
    if not result["duration_offer_presented"]:
        return {"allow": False, "status": "duration_offer_required"}
    if not result.get("selected_duration_minutes") or not result.get(
        "selected_service_name"
    ):
        return {"allow": False, "status": "duration_selection_required"}
    if kind == MASSAGE and not result["addon_offer_presented"]:
        return {"allow": False, "status": "addon_offer_required"}
    return {"allow": True, "status": "ready"}

