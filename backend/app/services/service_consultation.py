"""Deterministic, catalog-grounded spa consultation recommendations.

The realtime voice layer may collect a few short answers to help a caller choose
a treatment.  This module converts those answers into a broad, non-diagnostic
category and returns only services and add-ons that the establishment actually
configured.  Raw answers, including injury or medical details, are deliberately
excluded from every returned value.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.services.catalog_selection import (
    base_name, configured_duration, exact_catalog_service, family_variations,
)


FACIAL_SERVICE_TERMS: dict[str, tuple[str, ...]] = {
    "acne_clarifying": ("acne", "clarifying", "purifying", "deep cleansing"),
    "hydrating": ("hydrating", "hydration", "moisture", "dehydrated"),
    "calming_barrier_repair": (
        "calming",
        "barrier repair",
        "barrier-repair",
        "sensitive",
        "soothing",
    ),
    "custom": (
        "custom facial",
        "customized facial",
        "signature facial",
        "anti aging",
        "anti-aging",
        "age defying",
        "brightening facial",
    ),
}

MASSAGE_SERVICE_TERMS: dict[str, tuple[str, ...]] = {
    "relaxation": ("relaxation massage", "relaxing massage", "swedish massage"),
    "focused_therapeutic": (
        "focused massage",
        "therapeutic massage",
        "targeted massage",
    ),
    "deeper_pressure_sports": (
        "deeper pressure massage",
        "deep pressure massage",
        "deep tissue massage",
        "sports massage",
        "recovery massage",
    ),
    "customized": (
        "customized massage",
        "custom massage",
        "integrative massage",
    ),
}

ADD_ON_BENEFITS: dict[str, str] = {
    "hot_stones": (
        "Hot stones — they help the muscles relax more deeply and feel "
        "especially good on tight areas."
    ),
    "aromatherapy": (
        "Aromatherapy — we can use a calming blend or an energizing one, "
        "whichever you prefer."
    ),
    "cbd_oil": (
        "CBD-infused oil — many clients like it for extra tension and "
        "inflammation relief."
    ),
    "extended_scalp": (
        "A longer scalp massage — very relaxing and great for tension headaches."
    ),
    "foot_treatment": (
        "A relaxing foot treatment — nice way to finish and helps with overall "
        "circulation."
    ),
}

_ADD_ON_NAME_TERMS: dict[str, tuple[str, ...]] = {
    "hot_stones": ("hot stone", "hot stones"),
    "aromatherapy": ("aromatherapy", "aroma therapy"),
    "cbd_oil": ("cbd", "cbd oil", "cbd-infused oil", "cbd infused oil"),
    "extended_scalp": (
        "extended scalp",
        "scalp massage",
        "longer scalp massage",
    ),
    "foot_treatment": ("foot treatment", "foot massage", "feet treatment"),
}

_ADD_ON_NEED_TERMS: dict[str, tuple[str, ...]] = {
    "hot_stones": (
        "tight",
        "tension",
        "stiff",
        "sore",
        "muscle",
        "deep pressure",
        "deeper pressure",
    ),
    "aromatherapy": (
        "stress",
        "relax",
        "relaxation",
        "relaxed",
        "relaxing",
        "calm",
        "energy",
        "energized",
        "energizing",
        "fatigue",
    ),
    "cbd_oil": ("tension", "inflammation", "recovery", "pain", "sore"),
    "extended_scalp": ("scalp", "head tension", "headache", "migraine"),
    "foot_treatment": ("foot", "feet", "lower leg", "circulation"),
}

# A realtime follow-up deliberately does not retain the caller's free-form
# answers.  These category-level signals let it make the same conservative
# add-on decision after the caller chooses a duration without reintroducing
# potentially sensitive body/injury details into session state.
_MASSAGE_CATEGORY_ADD_ON_SIGNAL: dict[str, str] = {
    "relaxation": "relaxation",
    "focused_therapeutic": "tension",
    "deeper_pressure_sports": "deep pressure recovery",
}

_DURATION_RE = re.compile(
    r"\b(?:\d{1,3}|thirty|sixty|ninety)\s*[- ]*\s*(?:minutes?|mins?)\b",
    re.IGNORECASE,
)


def _text(*values: object) -> str:
    return " ".join(str(value).strip().casefold() for value in values if value).strip()


def _normalized(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _contains(text: str, terms: Iterable[str]) -> bool:
    normalized = f" {_normalized(text)} "
    return any(f" {_normalized(term)} " in normalized for term in terms)


def _service_name(service: Mapping[str, Any]) -> str:
    return str(service.get("name") or "").strip()


def _service_search_text(service: Mapping[str, Any]) -> str:
    aliases = service.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    # Explicit consultation metadata is supported when an account provides it,
    # but free-form descriptions are intentionally not used for routing.
    tags = service.get("consultation_tags") or service.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    category = service.get("consultation_category") or service.get("category")
    return _text(_service_name(service), *aliases, *tags, category)


def _copy_service(service: Mapping[str, Any]) -> dict[str, Any]:
    return dict(service)


def classify_facial_need(
    main_concern: str | None,
    skin_feel: str | None = None,
    sensitivity: str | None = None,
) -> str | None:
    """Return a broad cosmetic concern category without echoing raw answers."""

    combined = _text(main_concern, skin_feel, sensitivity)
    # Sensitivity wins when needs overlap so the recommendation remains gentle.
    if _contains(combined, ("sensitive", "sensitivity", "reactive", "redness", "red")):
        return "calming_barrier_repair"
    if _contains(
        combined,
        (
            "acne",
            "breakout",
            "breakouts",
            "oily",
            "oiliness",
            "congested",
            "congestion",
            "clogged",
        ),
    ):
        return "acne_clarifying"
    if _contains(
        combined,
        ("dry", "dryness", "dehydrated", "dehydration", "dull", "dullness", "flaky"),
    ):
        return "hydrating"
    if _contains(
        combined,
        (
            "aging",
            "ageing",
            "uneven",
            "maintenance",
            "fine line",
            "fine lines",
            "tone",
            "wrinkle",
            "wrinkles",
        ),
    ):
        return "custom"
    return None


def classify_massage_need(
    reason: str | None,
    areas: str | None = None,
    pressure: str | None = None,
) -> str | None:
    """Return a broad massage category; caller wording is never returned."""

    combined = _text(reason, areas, pressure)
    if _contains(
        combined,
        (
            "deep",
            "deeper",
            "deep pressure",
            "deeper pressure",
            "deep tissue",
            "sports",
            "recovery",
        ),
    ):
        return "deeper_pressure_sports"
    if _contains(
        combined,
        (
            "tension",
            "pain",
            "aches",
            "aching",
            "tight",
            "tightness",
            "sore",
            "soreness",
            "focused",
            "specific area",
            "specific areas",
        ),
    ):
        return "focused_therapeutic"
    if _contains(combined, ("relax", "relaxation", "stress", "unwind")):
        return "relaxation"
    if _contains(combined, ("mixed", "unsure", "not sure", "custom")):
        return "customized"
    # Naming a specific area is enough to prefer a focused treatment, but the
    # area itself is not copied into the result.
    if areas and _normalized(areas) not in {"none", "no", "nothing", "all over"}:
        return "focused_therapeutic"
    return None


def configured_service_for_category(
    services: Sequence[Mapping[str, Any]],
    *,
    category: str | None,
    service_kind: str,
) -> dict[str, Any] | None:
    """Choose one actual configured service; never synthesize a menu entry."""

    if not category:
        return None
    terms_by_kind = FACIAL_SERVICE_TERMS if service_kind == "facial" else MASSAGE_SERVICE_TERMS
    terms = terms_by_kind.get(category, ())
    for service in services:
        if not _service_name(service) or service.get("is_add_on"):
            continue
        modality_aliases = service.get("aliases") or []
        if isinstance(modality_aliases, str):
            modality_aliases = [modality_aliases]
        explicit_kind = _normalized(
            service.get("consultation_kind") or service.get("service_kind")
        )
        modality_text = _normalized(
            _text(
                _service_name(service),
                service.get("category"),
                service.get("service_family"),
                *modality_aliases,
            )
        )
        if explicit_kind:
            if explicit_kind != service_kind:
                continue
        elif service_kind not in modality_text:
            # A category word such as "hydrating" or "therapeutic" is not
            # enough to decide whether a catalog entry is a facial or massage.
            # Require explicit modality metadata or a modality-bearing menu
            # name/family so a misleading cross-service match fails closed.
            continue
        configured_category = _normalized(
            service.get("consultation_category") or service.get("category")
        ).replace(" ", "_")
        if configured_category == category or _contains(_service_search_text(service), terms):
            return _copy_service(service)
    return None


def _duration_minutes(service: Mapping[str, Any]) -> int | None:
    return configured_duration(service)


def _family(service: Mapping[str, Any]) -> str:
    # Keep owner-supplied base_service as a legacy family alias for upsells.
    explicit = (
        service.get("service_family")
        or service.get("family")
        or service.get("base_service")
    )
    return _normalized(explicit) if explicit else base_name(_service_name(service))


def _consultation_kind(service: Mapping[str, Any]) -> str | None:
    explicit = _normalized(
        service.get("consultation_kind") or service.get("service_kind")
    )
    if explicit in {"facial", "massage"}:
        return explicit
    modality_text = _normalized(
        _text(
            _service_name(service),
            service.get("category"),
            service.get("service_family"),
        )
    )
    facial = "facial" in modality_text.split()
    massage = "massage" in modality_text.split()
    if facial == massage:
        return None
    return "facial" if facial else "massage"


def configured_duration_choices(
    services: Sequence[Mapping[str, Any]],
    selected_service: Mapping[str, Any] | str | None,
) -> dict[str, Any]:
    """Offer only configured variations of the SAME treatment family.

    Durations are menu-driven (including 45, 75, 90 minutes), not assumed to
    be 30/60. A generic 'massage' must not pick an unrelated first service.
    """
    variants = family_variations(services, selected_service)
    choices = [
        {"minutes": _duration_minutes(service), "service": _copy_service(service)}
        for service in variants
        if _duration_minutes(service) is not None
    ]
    available = {item["minutes"] for item in choices}
    return {
        "choices": choices,
        # Kept for existing UI/tests; does not create hypothetical offerings.
        "missing_minutes": [m for m in (30, 60) if m not in available],
    }


def _matching_catalog_service(
    services: Sequence[Mapping[str, Any]], allowed_name: object
) -> Mapping[str, Any] | None:
    wanted = _normalized(allowed_name)
    if not wanted:
        return None
    exact = [service for service in services if _normalized(_service_name(service)) == wanted]
    if len(exact) == 1:
        return exact[0]
    return None


def _addon_key(service: Mapping[str, Any]) -> str | None:
    text = _service_search_text(service)
    return next(
        (key for key, terms in _ADD_ON_NAME_TERMS.items() if _contains(text, terms)),
        None,
    )


def relevant_configured_addons(
    services: Sequence[Mapping[str, Any]],
    upsell_rules: Sequence[Mapping[str, Any]],
    *,
    base_service: Mapping[str, Any] | str,
    stated_needs: Sequence[str | None] = (),
    limit: int = 2,
) -> list[dict[str, Any]]:
    """Return up to two relevant, owner-approved, catalog-present add-ons."""

    if limit <= 0:
        return []
    base_name = _service_name(base_service) if isinstance(base_service, Mapping) else str(base_service)
    base_keys = {_normalized(base_name)}
    if isinstance(base_service, Mapping):
        base_keys.add(_family(base_service))
    allowed_names: list[str] = []
    for rule in upsell_rules:
        if _normalized(rule.get("base_service")) not in base_keys:
            continue
        values = rule.get("allowed_upsells") or []
        if isinstance(values, str):
            values = [values]
        allowed_names.extend(str(value) for value in values)

    needs = _text(*stated_needs)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for allowed_name in allowed_names:
        service = _matching_catalog_service(services, allowed_name)
        if not service:
            continue
        key = _addon_key(service)
        if not key or key in seen or not _contains(needs, _ADD_ON_NEED_TERMS[key]):
            continue
        results.append(
            {
                "key": key,
                "service": _copy_service(service),
                "benefit": str(service.get("approved_benefit") or ADD_ON_BENEFITS[key]),
            }
        )
        seen.add(key)
        if len(results) >= min(limit, 2):
            break
    return results


def _selected_duration_service(
    durations: Mapping[str, Any],
    *,
    selected_service_name: str | None,
    selected_duration_minutes: int | None,
) -> dict[str, Any] | None:
    """Bind an add-on decision to one real caller-selected menu variation."""

    choices = durations.get("choices") or []
    wanted_name = _normalized(selected_service_name)
    wanted_minutes = (
        selected_duration_minutes
        if isinstance(selected_duration_minutes, int)
        and not isinstance(selected_duration_minutes, bool)
        and selected_duration_minutes > 0
        else None
    )
    matches = []
    for choice in choices:
        service = choice.get("service") if isinstance(choice, Mapping) else None
        if not isinstance(service, Mapping):
            continue
        if wanted_name and _normalized(_service_name(service)) != wanted_name:
            continue
        if wanted_minutes and choice.get("minutes") != wanted_minutes:
            continue
        matches.append(service)
    if not wanted_name and not wanted_minutes:
        return None
    return _copy_service(matches[0]) if len(matches) == 1 else None


def facial_consultation(
    services: Sequence[Mapping[str, Any]],
    *,
    main_concern: str | None,
    skin_feel: str | None = None,
    sensitivity: str | None = None,
    selected_service_name: str | None = None,
    selected_duration_minutes: int | None = None,
    category_hint: str | None = None,
) -> dict[str, Any]:
    category = classify_facial_need(main_concern, skin_feel, sensitivity) or category_hint
    service = configured_service_for_category(
        services, category=category, service_kind="facial"
    )
    durations = configured_duration_choices(services, service)
    selected = _selected_duration_service(
        durations,
        selected_service_name=selected_service_name,
        selected_duration_minutes=selected_duration_minutes,
    )
    return {
        "category": category,
        "service": service,
        "durations": durations,
        "selected_service": selected,
        "needs_clarification": category is None or service is None,
    }


def massage_consultation(
    services: Sequence[Mapping[str, Any]],
    upsell_rules: Sequence[Mapping[str, Any]],
    *,
    reason: str | None,
    areas: str | None = None,
    pressure: str | None = None,
    areas_to_avoid: str | None = None,
    selected_service_name: str | None = None,
    selected_duration_minutes: int | None = None,
    category_hint: str | None = None,
) -> dict[str, Any]:
    """Build a grounded result; ``areas_to_avoid`` is intentionally discarded."""

    del areas_to_avoid
    category = classify_massage_need(reason, areas, pressure) or category_hint
    service = configured_service_for_category(
        services, category=category, service_kind="massage"
    )
    durations = configured_duration_choices(services, service)
    selected = _selected_duration_service(
        durations,
        selected_service_name=selected_service_name,
        selected_duration_minutes=selected_duration_minutes,
    )
    return {
        "category": category,
        "service": service,
        "durations": durations,
        "selected_service": selected,
        "addons": relevant_configured_addons(
            services,
            upsell_rules,
            base_service=selected or "",
            stated_needs=(
                reason,
                areas,
                pressure,
                _MASSAGE_CATEGORY_ADD_ON_SIGNAL.get(category or ""),
            ),
        ) if selected else [],
        "needs_clarification": category is None or service is None,
    }
