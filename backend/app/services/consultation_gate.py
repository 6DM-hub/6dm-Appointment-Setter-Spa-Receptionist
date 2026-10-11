"""Pure, server-owned state transitions for spa service consultations.

The realtime model can phrase a transition returned by this module, but it must
not decide whether the consultation is complete.  Every helper accepts and
returns JSON-safe dictionaries so the voice layer can keep the state in the
existing call-session entity bag.

Free-form consultation answers are used only during the current function call.
They are reduced to an answered-field marker and a broad cosmetic/wellness
category.  Facial answers also produce a small allow-listed summary that can
be shared with the esthetician in the booking note.  In particular, raw
answers, injury details, and area-to-avoid text are never copied into the
returned state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.services.service_consultation import (
    classify_massage_need,
    configured_duration_choices,
    relevant_configured_addons,
)


FACIAL = "facial"
MASSAGE = "massage"
CONSULTATION_KINDS = frozenset({FACIAL, MASSAGE})

FACIAL_PERMISSION_PROMPT = (
    "Of course. May I ask you a few quick questions about your skin so I can "
    "relay the details to your esthetician for your appointment?"
)

REQUIRED_QUESTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    FACIAL: (
        (
            "main_concern",
            "What's the main thing bothering you about your skin right now?",
        ),
        (
            "skin_feel",
            "How does your skin usually feel by the middle of the day, or even at the end of the day? Is it more oily? Is it dry? Does it feel tight? Combination? Or pretty balanced?",
        ),
        (
            "active_breakouts",
            "Are you dealing with any active breakouts right now?",
        ),
        (
            "skin_sensitivity",
            "Do you have any sensitivity or redness, or are there products that tend to irritate your skin?",
        ),
        (
            "facial_history",
            "Have you had facials before, and is there anything you especially liked or did not like?",
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

_FACIAL_CONCERN_TERMS: dict[str, tuple[str, ...]] = {
    "acne": ("acne",),
    "breakouts": ("breakout", "breakouts", "pimple", "pimples"),
    "oiliness": ("oily", "oiliness", "greasy"),
    "dryness": ("dry", "dryness", "dehydrated", "dehydration", "flaky"),
    "dullness": ("dull", "dullness"),
    "sensitivity": ("sensitive", "sensitivity", "reactive"),
    "redness": ("red", "redness"),
    "aging or fine lines": (
        "aging",
        "ageing",
        "fine line",
        "fine lines",
        "wrinkle",
        "wrinkles",
    ),
    "clogged pores": ("clogged pore", "clogged pores", "congested", "congestion"),
    "uneven tone": ("uneven tone", "dark spot", "dark spots", "pigmentation"),
}
_SKIN_FEEL_TERMS: dict[str, tuple[str, ...]] = {
    "oily": ("oily", "oiliness", "greasy"),
    "dry": ("dry", "dryness", "dehydrated", "flaky"),
    "tight": ("tight", "tightness"),
    "combination": ("combination", "both oily and dry", "oily and dry"),
    "balanced": ("balanced", "normal"),
}
_FACIAL_PREFERENCE_TERMS: dict[str, tuple[str, ...]] = {
    "extractions": ("extraction", "extractions"),
    "steam": ("steam", "steaming"),
    "facial massage": ("facial massage", "face massage"),
    "gentle hydration": ("gentle hydration", "hydration", "hydrating"),
    "exfoliation": ("exfoliation", "exfoliating", "scrub", "scrubs", "peel", "peels"),
    "fragrance": ("fragrance", "fragranced", "scented products"),
    "masks": ("mask", "masks"),
}
_NEGATIVE_RESPONSE_RE = re.compile(
    r"\b(?:no|none|not|don't|do not|doesn't|does not|haven't|have not|never|without|nope)\b",
    re.IGNORECASE,
)
_POSITIVE_HISTORY_RE = re.compile(
    r"\b(?:yes|have|had|before|previous|previously)\b", re.IGNORECASE
)
_LIKED_RE = re.compile(r"\b(?:like|liked|love|loved|enjoy|enjoyed|prefer|preferred)\b", re.IGNORECASE)
_DISLIKED_RE = re.compile(r"\b(?:dislike|disliked|hate|hated|did not like|didn't like|avoid)\b", re.IGNORECASE)

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


def _contains_term(text: str, terms: Sequence[str]) -> bool:
    normalized = f" {_normalized(text)} "
    return any(f" {_normalized(term)} " in normalized for term in terms)


def _safe_facial_summary(value: object) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    allowed_concerns = set(_FACIAL_CONCERN_TERMS)
    allowed_feel = set(_SKIN_FEEL_TERMS)
    allowed_preferences = set(_FACIAL_PREFERENCE_TERMS)
    breakout_status = str(raw.get("active_breakouts") or "").strip()
    sensitivity_status = str(raw.get("sensitivity") or "").strip()
    prior_facials = str(raw.get("prior_facials") or "").strip()
    return {
        "concerns": _clean_string_list(
            raw.get("concerns"), allowed=allowed_concerns
        ),
        "skin_feel": _clean_string_list(
            raw.get("skin_feel"), allowed=allowed_feel
        ),
        "active_breakouts": (
            breakout_status if breakout_status in {"reported", "not reported"} else None
        ),
        "sensitivity": (
            sensitivity_status
            if sensitivity_status in {"reported", "not reported"}
            else None
        ),
        "prior_facials": (
            prior_facials if prior_facials in {"yes", "no", "unclear"} else None
        ),
        "liked": _clean_string_list(raw.get("liked"), allowed=allowed_preferences),
        "avoids": _clean_string_list(raw.get("avoids"), allowed=allowed_preferences),
    }


def _append_unique(target: list[str], values: Sequence[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _negative_for(text: str, terms: Sequence[str]) -> bool:
    normalized = _normalized(text)
    negative = r"(?:no|not|without|never|don t|do not|doesn t|does not|haven t|have not|isn t|is not)"
    bridge = r"(?:\s+(?:have|had|dealing with|experiencing|seeing|any|active))*"
    for term in terms:
        normalized_term = _normalized(term)
        token = re.escape(normalized_term)
        direct = re.search(
            rf"\b{negative}{bridge}\s+{token}\b",
            normalized,
        )
        if direct:
            return True
        position = normalized.find(normalized_term)
        if position < 0:
            continue
        nearby = normalized[max(0, position - 55):position]
        negatives = list(re.finditer(rf"\b{negative}\b", nearby))
        contrasts = list(re.finditer(r"\b(?:but|however|although)\b", nearby))
        if negatives and (
            not contrasts or negatives[-1].start() > contrasts[-1].start()
        ):
            return True
    return False


def _reported_status(text: str, terms: Sequence[str]) -> str | None:
    """Classify a signal group while respecting mixed yes/no clauses."""

    mentioned = [term for term in terms if _contains_term(text, (term,))]
    if not mentioned:
        return None
    if any(not _negative_for(text, (term,)) for term in mentioned):
        return "reported"
    return "not reported"


def _update_facial_summary(
    summary: Mapping[str, Any] | None,
    *,
    field: str,
    answer: str,
) -> dict[str, Any]:
    """Reduce one answer to non-diagnostic, allow-listed staff details."""

    result = _safe_facial_summary(summary)
    text = str(answer or "").strip()
    if not text:
        return result
    unsure = bool(
        re.search(
            r"\b(?:not sure|unsure|don't know|do not know|hard to say)\b",
            text,
            re.IGNORECASE,
        )
    )

    concerns = [
        label
        for label, terms in _FACIAL_CONCERN_TERMS.items()
        if _contains_term(text, terms) and not _negative_for(text, terms)
    ]
    feel = [
        label
        for label, terms in _SKIN_FEEL_TERMS.items()
        if _contains_term(text, terms) and not _negative_for(text, terms)
    ]
    _append_unique(result["concerns"], concerns)
    _append_unique(result["skin_feel"], feel)

    breakout_terms = (
        "acne",
        "breakout",
        "breakouts",
        "pimple",
        "pimples",
        "clogged pores",
    )
    breakout_status = _reported_status(text, breakout_terms)
    if unsure and field == "active_breakouts":
        result["active_breakouts"] = None
    elif field == "active_breakouts" or breakout_status:
        result["active_breakouts"] = breakout_status or (
            "not reported" if _NEGATIVE_RESPONSE_RE.search(text) else "reported"
        )

    sensitivity_terms = (
        "sensitive",
        "sensitivity",
        "reactive",
        "red",
        "redness",
        "irritate",
        "irritates",
        "irritation",
    )
    sensitivity_status = _reported_status(text, sensitivity_terms)
    if unsure and field == "skin_sensitivity":
        result["sensitivity"] = None
    elif field == "skin_sensitivity" or sensitivity_status:
        result["sensitivity"] = sensitivity_status or (
            "not reported" if _NEGATIVE_RESPONSE_RE.search(text) else "reported"
        )

    if field == "facial_history":
        normalized_history = _normalized(text)
        no_prior = bool(
            re.search(
                r"\b(?:no|never|haven't|have not)\b.{0,24}\b(?:facial|facials)\b",
                text,
                re.IGNORECASE,
            )
            or normalized_history in {"no", "nope", "not yet", "never"}
            or re.search(
                r"\b(?:my )?first\s+(?:facial|one|time)\b",
                normalized_history,
            )
        )
        result["prior_facials"] = (
            "no" if no_prior else "yes" if _POSITIVE_HISTORY_RE.search(text) else "unclear"
        )
        normalized = _normalized(text)
        for label, terms in _FACIAL_PREFERENCE_TERMS.items():
            if not _contains_term(text, terms):
                continue
            positions = [normalized.find(_normalized(term)) for term in terms]
            positions = [position for position in positions if position >= 0]
            position = min(positions) if positions else len(normalized)
            nearby = normalized[max(0, position - 45):position]
            if _DISLIKED_RE.search(nearby) or re.search(
                r"\b(?:no|not|without|harsh)\b", nearby, re.IGNORECASE
            ):
                _append_unique(result["avoids"], [label])
            elif _LIKED_RE.search(text):
                _append_unique(result["liked"], [label])
    return result


def facial_consultation_staff_note(state: Mapping[str, Any] | None) -> str | None:
    """Return a bounded staff note containing only allow-listed facial details."""

    if not isinstance(state, Mapping) or state.get("permission_status") != "accepted":
        return None
    summary = _safe_facial_summary(state.get("facial_summary"))
    parts: list[str] = []
    if summary["concerns"]:
        parts.append("concerns: " + ", ".join(summary["concerns"]))
    if summary["skin_feel"]:
        parts.append("skin feel: " + ", ".join(summary["skin_feel"]))
    if summary["active_breakouts"]:
        parts.append("active breakouts: " + summary["active_breakouts"])
    if summary["sensitivity"]:
        parts.append("sensitivity/redness/product irritation: " + summary["sensitivity"])
    if summary["prior_facials"]:
        parts.append("prior facials: " + summary["prior_facials"])
    if summary["liked"]:
        parts.append("liked: " + ", ".join(summary["liked"]))
    if summary["avoids"]:
        parts.append("prefers to avoid: " + ", ".join(summary["avoids"]))
    return "Facial consultation — " + "; ".join(parts) + "." if parts else None


def _facial_category_from_summary(summary: Mapping[str, Any] | None) -> str | None:
    """Choose a broad cosmetic category from polarity-aware safe signals."""

    safe = _safe_facial_summary(summary)
    concerns = set(safe["concerns"])
    feel = set(safe["skin_feel"])
    if safe["sensitivity"] == "reported" or concerns & {"sensitivity", "redness"}:
        return "calming_barrier_repair"
    if safe["active_breakouts"] == "reported" or concerns & {
        "acne",
        "breakouts",
        "oiliness",
        "clogged pores",
    }:
        return "acne_clarifying"
    if concerns & {"dryness", "dullness"} or feel & {"dry", "tight"}:
        return "hydrating"
    if concerns & {"aging or fine lines", "uneven tone"}:
        return "custom"
    return None


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
    permission_status = str(prior.get("permission_status") or "").strip()
    if permission_status not in {"accepted", "declined"}:
        permission_status = ""
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
        "permission_status": permission_status or None,
        "facial_summary": (
            _safe_facial_summary(prior.get("facial_summary"))
            if kind == FACIAL
            else {}
        ),
    }
    return safe


def record_consultation_permission(
    state: Mapping[str, Any], *, accepted: bool
) -> dict[str, Any]:
    """Record a caller's answer to the optional facial-question invitation."""

    kind = str(state.get("kind") or "")
    if kind != FACIAL:
        raise ValueError("consultation permission applies only to facial consultations")
    result = _safe_prior_state(state, kind)
    result["permission_status"] = "accepted" if accepted else "declined"
    if not accepted:
        # Declining the optional questions must never prevent an otherwise
        # valid facial booking.  Mark only the question fields complete; no
        # answer detail or recommendation category is fabricated.
        result["answered_fields"] = [
            field for field, _question in REQUIRED_QUESTIONS[FACIAL]
        ]
    return result


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
            result["facial_summary"] = _update_facial_summary(
                result.get("facial_summary"), field=field, answer=raw
            )
            category = _facial_category_from_summary(result["facial_summary"])
        else:
            category = classify_massage_need(raw)
    result["category"] = _merge_category(kind, result.get("category"), category)
    return result


def record_volunteered_facial_details(
    state: Mapping[str, Any], *, caller_utterance: str
) -> dict[str, Any]:
    """Credit clear facial details already volunteered in the same turn.

    This prevents Cara from asking about a breakout or sensitivity immediately
    after the caller just mentioned it.  Only strong allow-listed signals are
    credited, and the caller's raw wording is discarded.
    """

    if state.get("kind") != FACIAL:
        return dict(state)
    result = _safe_prior_state(state, FACIAL)
    text = str(caller_utterance or "").strip()
    if not text:
        return result
    fields: list[str] = []
    if any(
        _contains_term(text, terms) and not _negative_for(text, terms)
        for terms in _FACIAL_CONCERN_TERMS.values()
    ):
        fields.append("main_concern")
    if any(_contains_term(text, terms) for terms in _SKIN_FEEL_TERMS.values()):
        fields.append("skin_feel")
    breakout_terms = ("acne", "breakout", "breakouts", "pimple", "pimples", "clogged pores")
    if _contains_term(text, breakout_terms):
        fields.append("active_breakouts")
    sensitivity_terms = (
        "sensitive",
        "sensitivity",
        "reactive",
        "red",
        "redness",
        "irritate",
        "irritates",
        "irritation",
    )
    if _contains_term(text, sensitivity_terms):
        fields.append("skin_sensitivity")
    mentions_prior_facial = bool(
        re.search(
            r"\b(?:have|had|gotten|received)\b.{0,28}\bfacials?\b|"
            r"\bfacials?\b.{0,28}\b(?:before|previously|last time)\b",
            text,
            re.IGNORECASE,
        )
    )
    mentions_facial_preference = bool(
        (_LIKED_RE.search(text) or _DISLIKED_RE.search(text))
        and any(
            _contains_term(text, terms)
            for terms in _FACIAL_PREFERENCE_TERMS.values()
        )
    )
    if mentions_prior_facial or mentions_facial_preference:
        fields.append("facial_history")

    for field in fields:
        result = record_consultation_answer(result, field=field, answer=text)
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
    if kind == FACIAL:
        if re.search(
            r"\b(?:not sure|unsure|don't know|do not know|hard to say)\b",
            utterance,
            re.IGNORECASE,
        ):
            # Uncertainty is a complete, safe answer to an optional question.
            # Advance without making the caller repeat it or inferring a fact.
            result = record_consultation_answer(
                result,
                field=pending,
                answer="unsure",
            )
            return {
                "state": result,
                "status": "answer_recorded",
                "answered_field": pending,
            }
        if not _facial_answer_recognized(pending, utterance):
            return {
                "state": result,
                "status": "unrecognized_answer",
                "answered_field": pending,
            }
    result = record_consultation_answer(
        result,
        field=pending,
        answer=utterance,
    )
    if kind == FACIAL:
        result = record_volunteered_facial_details(
            result, caller_utterance=utterance
        )
    return {
        "state": result,
        "status": "answer_recorded",
        "answered_field": pending,
    }


def _facial_answer_recognized(field: str, utterance: str) -> bool:
    text = str(utterance or "").strip()
    if not text:
        return False
    if re.search(
        r"\b(?:repeat (?:that|the question)|say (?:that|the question) again|"
        r"didn't hear|did not hear|what was the question|come again|pardon)\b",
        text,
        re.IGNORECASE,
    ) or re.fullmatch(r"\s*(?:what|huh|sorry)\s*[?.!]*\s*", text, re.IGNORECASE):
        return False
    unsure = bool(
        re.search(
            r"\b(?:not sure|unsure|don't know|do not know|hard to say)\b",
            text,
            re.IGNORECASE,
        )
    )
    if unsure:
        return False
    normalized = _normalized(text)
    bare_polar = bool(
        re.fullmatch(
            r"(?:yes|yeah|yep|no|nope|none|never|not really|"
            r"i do|i don't|i do not|i have|i haven't|i have not)",
            normalized,
        )
    )
    if field == "main_concern":
        if re.fullmatch(
            r"(?:what|huh|pardon|sorry|okay|ok|yes|yeah|yep|sure)",
            normalized,
        ):
            return False
        return bool(
            normalized in {"no", "none", "nothing", "nothing in particular"}
            or len(normalized.split()) >= 1
        )
    if field == "skin_feel":
        return any(
            _contains_term(text, terms) for terms in _SKIN_FEEL_TERMS.values()
        )
    if field == "active_breakouts":
        return bare_polar or _reported_status(
            text, ("acne", "breakout", "breakouts", "pimple", "pimples", "clogged pores")
        ) is not None
    if field == "skin_sensitivity":
        return bare_polar or _reported_status(
            text,
            (
                "sensitive",
                "sensitivity",
                "reactive",
                "red",
                "redness",
                "irritate",
                "irritates",
                "irritation",
                "product",
                "products",
            ),
        ) is not None
    if field == "facial_history":
        return bool(
            bare_polar
            or re.search(
                r"\b(?:first|before|previously|last time|have had|had a|had facials?|liked|loved|disliked|did not like|didn't like)\b",
                text,
                re.IGNORECASE,
            )
        )
    return True


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
        name = " ".join(_DURATION_RE.sub("", name, count=1).strip(" -").split()) or name
        benefit = (
            "is a focused start"
            if kind == FACIAL
            else "can focus well on specific areas"
        )
        parts.append(f"The 30-minute {name} {benefit}.")
    if 60 in by_minutes:
        name = _service_name(by_minutes[60]["service"])
        name = " ".join(_DURATION_RE.sub("", name, count=1).strip(" -").split()) or name
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

