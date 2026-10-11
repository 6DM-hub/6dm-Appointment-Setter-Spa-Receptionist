"""Shared, conservative spa menu selection for every booking service.

This module matches actual configured entries without inventing variations,
prices or providers. A broad treatment family (e.g. "Swedish Massage") may
produce duration choices, but never silently chooses one variation.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_DURATION = re.compile(
    r"\b(?P<minutes>\d{1,3}|thirty|sixty|ninety)\s*[- ]*\s*"
    r"(?:minutes?|mins?)\b",
    re.IGNORECASE,
)
_SPOKEN_MINUTES = {"thirty": 30, "sixty": 60, "ninety": 90}


def normalized(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def duration_from_name(value: object) -> int | None:
    found = _DURATION.search(str(value or ""))
    if found is None:
        return None
    token = found.group("minutes").casefold()
    minutes = _SPOKEN_MINUTES.get(token)
    if minutes is None:
        minutes = int(token)
    return minutes if 1 <= minutes <= 480 else None


def configured_duration(service: Mapping[str, Any] | str | None) -> int | None:
    if isinstance(service, Mapping):
        value = service.get("duration_minutes")
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 480:
            return value
        return duration_from_name(service.get("name"))
    return duration_from_name(service)


def base_name(value: object) -> str:
    return normalized(_DURATION.sub(" ", str(value or ""), count=1))


def service_family(service: Mapping[str, Any]) -> str:
    explicit = service.get("service_family") or service.get("family")
    return normalized(explicit) if explicit else base_name(service.get("name"))


def _aliases(service: Mapping[str, Any]) -> list[str]:
    aliases = service.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    if not isinstance(aliases, (list, tuple)):
        return []
    return [normalized(alias) for alias in aliases if normalized(alias)]


def exact_catalog_service(
    services: Sequence[Mapping[str, Any]], requested: object
) -> Mapping[str, Any] | None:
    """Return a uniquely identified variation, never just the first match.

    Exact names and configured aliases are considered; an explicit duration
    may disambiguate a request like "90-minute Swedish Massage" against
    "Swedish Massage" variants. Without a duration, multiple same-family
    variations stay ambiguous so the caller can choose.
    """
    text = normalized(requested)
    if not text:
        return None
    exact = [
        service for service in services
        if normalized(service.get("name")) == text or text in _aliases(service)
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    requested_minutes = duration_from_name(requested)
    requested_family = base_name(requested)
    if not requested_family:
        return None
    matching = [
        service for service in services
        if requested_family == service_family(service)
        or requested_family == base_name(service.get("name"))
        or requested_family in _aliases(service)
    ]
    if requested_minutes is not None:
        matching = [
            service for service in matching
            if configured_duration(service) == requested_minutes
        ]
    return matching[0] if len(matching) == 1 else None


def family_variations(
    services: Sequence[Mapping[str, Any]], requested: object
) -> list[Mapping[str, Any]]:
    """Offer only variations of the requested menu family, never substitutes.

    For a broad modal request ("massage", "facial"), no family is selected
    until the caller chooses one or a configured recommendation is offered.
    """
    if isinstance(requested, Mapping):
        family = service_family(requested)
    else:
        text = normalized(requested)
        if text in {"massage", "massages", "facial", "facials", "wax", "waxing", "service", "treatment"}:
            return []
        found = exact_catalog_service(services, requested)
        family = service_family(found) if found is not None else base_name(requested)
    if not family:
        return []
    choices = [
        service for service in services
        if not service.get("is_add_on")
        and (service_family(service) == family or base_name(service.get("name")) == family)
        and configured_duration(service) is not None
    ]
    by_duration: dict[int, Mapping[str, Any]] = {}
    for service in choices:
        minutes = configured_duration(service)
        if minutes is not None and minutes not in by_duration:
            by_duration[minutes] = service
    return [by_duration[m] for m in sorted(by_duration)]
