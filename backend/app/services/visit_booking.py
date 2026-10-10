"""Consecutive visits backed by Square's atomic multi-segment booking API.

No independently discovered slots are joined and no partial local success is
reported. Provider-managed buffers/resources must be present in availability.
"""
import itertools
import json
import re
from dataclasses import replace
from datetime import timedelta, date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, ConfigDict, field_validator

from app.services.booking_adapters.base import AvailabilityVerdict, BookingProviderError, ExternalBooking
from app.services.business_hours import is_open_between
from app.services.scheduling_time import business_zone, utc_instant, elapsed_end


class VisitPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    allow_reorder: bool = False
    allow_after_hours: bool = False
    after_hours: dict = Field(default_factory=dict)
    special_hours: dict = Field(default_factory=dict)
    max_services: int = Field(6, ge=2, le=6)

    @field_validator("after_hours", "special_hours")
    @classmethod
    def valid_windows(cls, value, info):
        for day, windows in value.items():
            if info.field_name == "special_hours":
                date.fromisoformat(day)
            elif day not in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
                raise ValueError("Invalid weekday")
            if not isinstance(windows, list):
                raise ValueError("Hours must be a list of opening windows")
            for window in windows:
                if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(window.get(key, ""))) for key in ("open", "close")) or window["open"] >= window["close"]:
                    raise ValueError("Opening windows require valid increasing HH:MM times")
        return value


def parts(router, description):
    text = (description or "").strip()
    # Match complete catalog labels before splitting conjunctions. Duration
    # prefixes/suffixes must not turn "Face and Neck" into two treatments.
    def label(value):
        value = re.sub(r"\b\d+(?:\.\d+)?\s*[-–]?\s*(?:minutes?|mins?|hours?|hrs?)\b", "", value, flags=re.I)
        return " ".join(re.findall(r"[\w]+|[+&]", value.casefold()))
    if any(label(str(entry.get("name") or "")) == label(text) for entry in router.spa.services or []):
        return [text]
    explicit = re.split(r"\s*(?:\+|;|\bthen\b)\s*", text, flags=re.I)
    if len(explicit) > 1:
        return [item for part in explicit if part.strip() for item in parts(router, part)]
    tokens = re.split(r"\s+and\s+", text, flags=re.I)
    catalog = {label(str(entry.get("name") or "")) for entry in router.spa.services or []}
    # Prefer the longest catalog label, preserving conjunctions within names
    # even when another treatment follows it.
    result = []
    offset = 0
    while offset < len(tokens):
        stop = next((end for end in range(len(tokens), offset, -1)
                     if label(" and ".join(tokens[offset:end])) in catalog), offset + 1)
        result.append(" and ".join(tokens[offset:stop]).strip())
        offset = stop
    return [item for item in result if item]


def policy(router):
    return VisitPolicy.model_validate((getattr(router.spa, "booking_policies", None) or {}).get("visit", {}))


def in_hours(hours, special, timezone, start, end):
    local = start.astimezone(ZoneInfo(timezone))
    windows = special.get(local.date().isoformat())
    if windows is not None:
        hours = {local.strftime("%a").lower(): windows}
    return bool(hours) and is_open_between(hours, timezone, start, end)


def validate_sequence(raw, specs, router):
    """Return a pinned complete visit, or None; never guess provider fields."""
    delegate = router.delegate
    segments = raw.get("appointment_segments") or []
    if len(segments) != len(specs) or raw.get("location_id") != delegate.location_id:
        return None
    if not raw.get("start_at"):
        raise BookingProviderError("Provider availability is missing its start time.", code="INVALID_RESPONSE")
    try:
        start = utc_instant(delegate._parse_square_datetime(raw["start_at"]))
    except (TypeError, ValueError) as exc:
        raise BookingProviderError("Provider availability has an invalid timestamp.", code="INVALID_RESPONSE") from exc
    cursor = start
    visit = []
    p = policy(router)
    for segment, spec in zip(segments, specs):
        minutes = int(segment.get("duration_minutes") or 0)
        gap = int(segment.get("intermission_minutes") or 0)
        if (minutes <= 0 or gap < 0 or minutes != spec["minutes"]
                or segment.get("service_variation_id") != spec["id"]
                or segment.get("service_variation_version") != spec["version"]
                or not segment.get("team_member_id")):
            return None
        if spec["staff"] and segment["team_member_id"] not in spec["staff"]:
            return None
        if not set(spec["resources"]).issubset(segment.get("resource_ids") or []):
            return None
        # Buffers cannot be invented locally: the provider must reserve them.
        if gap < spec["buffer"] or spec["preparation"]:
            return None
        end = elapsed_end(cursor, minutes)
        occupied_end = elapsed_end(end, gap)
        staff = spec["staff_records"].get(segment["team_member_id"])
        if staff and (staff.get("hours") or staff.get("special_hours")) and not in_hours(staff.get("hours"), staff.get("special_hours") or {}, router.spa.timezone, cursor, occupied_end):
            return None
        visit.append({**segment, "service_name": spec["name"], "provider_name": (staff or {}).get("name"), "start": cursor.isoformat(), "end": end.isoformat()})
        cursor = occupied_end
    if not in_hours(router.spa.business_hours, p.special_hours, router.spa.timezone, start, cursor):
        if not p.allow_after_hours or not p.after_hours or not in_hours(p.after_hours, {}, router.spa.timezone, start, cursor):
            return None
    first = visit[0]
    return {"start": start.isoformat(), "location_id": raw["location_id"],
        "duration_minutes": int((cursor - start).total_seconds() / 60),
        "treatment_minutes": sum(item["duration_minutes"] for item in visit),
        "team_member_id": first["team_member_id"], "service_variation_id": first["service_variation_id"],
        "service_variation_version": first["service_variation_version"], "visit_segments": visit}


async def resolve_specs(router, ctx):
    requested = parts(router, ctx.service_description or ctx.title)
    p = policy(router)
    if (not p.enabled and len(requested) > 1) or len(requested) > p.max_services:
        raise BookingProviderError("This business does not permit that multi-service visit.", code="VISIT_POLICY")
    if not router.spa.business_hours and not p.special_hours:
        raise BookingProviderError("Business hours must be configured before a complete visit can be verified.", code="VISIT_HOURS")
    if router.delegate.provider != "square":
        raise BookingProviderError("This booking integration does not support a verified complete visit. Staff assistance is required.", code="VISIT_UNSUPPORTED")
    specs = []
    for name in requested:
        resolution = router.resolve_service(name)
        if resolution.status in {"ambiguous", "unspecified"}:
            raise BookingProviderError(router._clarification_reason(resolution), code="VISIT_SERVICE_CLARIFICATION")
        entry = resolution.entry if resolution.status == "resolved" else {}
        configured_minutes = entry.get("duration_minutes") if entry else None
        segment_minutes = (
            router.requested_duration_minutes(name)
            or (configured_minutes if isinstance(configured_minutes, int) and configured_minutes > 0 else None)
            or router.duration_for_service(name)
        )
        single = replace(ctx, title=name, service_description=name, selected_slot=None,
                         service_variation_id=None, service_variation_version=None, provider_id=None,
                         end=elapsed_end(ctx.start, segment_minutes))
        if entry:
            single = router._apply_entry(single, entry)
        single, blocked = router._staff_context(single)
        if blocked:
            raise BookingProviderError(blocked, code="VISIT_STAFF")
        service = await router.delegate._resolve_service_variation(single)
        # Retrieve the actual catalog variation even when a local alias provided its ID.
        data = await router.delegate._request("GET", f"/v2/catalog/object/{service['id']}")
        obj = data.get("object") or {}
        variation = obj.get("item_variation_data") or {}
        minutes = int(variation.get("service_duration") or 0) // 60000
        if obj.get("id") != service["id"] or not minutes or obj.get("version") is None or not variation.get("available_for_booking"):
            raise BookingProviderError("The service duration or bookable catalog variation could not be verified.", code="VISIT_CATALOG")
        names = [single.preferred_staff] if single.preferred_staff else list(single.allowed_staff or [])
        staff = [await router.delegate._resolve_preferred_team_member(n) for n in names]
        records = {}
        for member in router.spa.staff or []:
            if member.get("provider_id") or member.get("hours") or member.get("special_hours"):
                member_id = member.get("provider_id") or await router.delegate._resolve_preferred_team_member(member["name"])
                records[member_id] = member
        entry = entry or {}
        specs.append({"id": service["id"], "version": obj["version"], "name": resolution.name or name,
            "minutes": minutes, "staff": staff, "staff_records": records,
            "resources": entry.get("resource_ids") or [],
            "buffer": int(entry.get("transition_buffer_minutes") or 0) + int(entry.get("cleanup_buffer_minutes") or 0),
            "preparation": int(entry.get("preparation_buffer_minutes") or 0)})
    return specs


async def list_visits(router, ctx, range_start, range_end):
    business_zone(router.spa.timezone)
    range_start, range_end = utc_instant(range_start), utc_instant(range_end)
    range_start = max(range_start, datetime.now(timezone.utc))
    if range_start >= range_end:
        return []
    specs = await resolve_specs(router, ctx)
    delegate = router.delegate
    await delegate._location()
    # Never silently reinterpret local times using a different provider location.
    if delegate.timezone_name and delegate.timezone_name != router.spa.timezone:
        raise BookingProviderError("The dashboard and booking provider timezones differ. Please correct the business settings.", code="VISIT_TIMEZONE")
    orders = itertools.islice(itertools.permutations(specs), 24) if policy(router).allow_reorder else [specs]
    result = []
    for order in orders:
        filters = [{"service_variation_id": item["id"], **({"team_member_id_filter": {"any": item["staff"]}} if item["staff"] else {})} for item in order]
        payload = {"query": {"filter": {"location_id": delegate.location_id,
            "start_at_range": {"start_at": delegate._square_datetime(range_start),
                "end_at": delegate._square_datetime(max(range_end, range_start + timedelta(days=1)))},
            "segment_filters": filters}}}
        data = await delegate._request("POST", "/v2/bookings/availability/search", json=payload)
        if not isinstance(data, dict) or data.get("errors") or not isinstance(data.get("availabilities", []), list):
            raise BookingProviderError("The provider availability response could not be verified.", code="INVALID_RESPONSE")
        for raw in data.get("availabilities", []):
            if not isinstance(raw, dict):
                raise BookingProviderError("Invalid provider availability entry.", code="INVALID_RESPONSE")
            slot = validate_sequence(raw, order, router)
            if slot and range_start <= delegate._parse_square_datetime(slot["start"]) < range_end:
                if slot not in result:
                    result.append(slot)
    return sorted(result, key=lambda item: item["start"])


async def check_visit(router, ctx):
    try:
        local_start = ctx.start.astimezone(ZoneInfo(router.spa.timezone))
        day_start = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
        # Include earlier openings on the same day when the requested sequence
        # would run past closing, but never offer an opening in the past.
        slots = await list_visits(router, ctx, max(day_start, datetime.now(timezone.utc)), day_start + timedelta(days=1))
    except BookingProviderError as exc:
        if exc.code == "VISIT_SERVICE_CLARIFICATION":
            return AvailabilityVerdict.no(str(exc))
        raise
    except ValueError as exc:
        raise BookingProviderError("The business schedule or provider response could not be verified.", code="VISIT_CONFIGURATION") from exc
    pinned = ctx.selected_slot
    for slot in slots:
        if router.delegate._parse_square_datetime(slot["start"]) == ctx.start:
            if not pinned or slot == pinned:
                return AvailabilityVerdict.ok(slot)
    closest = sorted(slots, key=lambda item: abs((router.delegate._parse_square_datetime(item["start"]) - ctx.start).total_seconds()))
    return AvailabilityVerdict.no("The complete visit does not fit the verified business, staff, buffer and resource schedule.", alternatives=tuple(closest[:3]))


async def create_visit(router, ctx):
    delegate = router.delegate
    if not ctx.selected_slot or not ctx.selected_slot.get("visit_segments"):
        raise BookingProviderError("Select and confirm a verified complete visit first.", code="VISIT_NOT_PINNED")
    verdict = await check_visit(router, ctx)
    if not verdict.available:
        raise BookingProviderError("The complete visit is no longer available.", code="VISIT_CONFLICT")
    slot = verdict.slot
    customer_id = await delegate._get_or_create_customer(ctx)
    segments = [{k: item[k] for k in ("duration_minutes", "service_variation_id", "service_variation_version", "team_member_id")} for item in slot["visit_segments"]]
    key = delegate._idempotency_key("visit", ctx.booking_reference or ctx.customer_phone,
        slot["start"], delegate.location_id, json.dumps(segments, sort_keys=True))
    payload = {"idempotency_key": key, "booking": {"location_id": delegate.location_id,
        "start_at": delegate._square_datetime(ctx.start), "customer_id": customer_id, "appointment_segments": segments}}
    from app.services.square_booking_recovery import create_with_recovery
    data = await create_with_recovery(delegate, payload)
    record = data.get("booking") or {}
    booking_id = record.get("id")
    if not booking_id:
        raise BookingProviderError("The provider did not return a visit booking ID.", code="BOOKING_OUTCOME_UNKNOWN")
    actual = record.get("appointment_segments") or []
    expected = slot["visit_segments"]
    try:
        returned_start = delegate._parse_square_datetime(record.get("start_at") or "")
    except (TypeError, ValueError):
        returned_start = None
    exact = (record.get("status") == "ACCEPTED" and record.get("location_id") == delegate.location_id
        and record.get("customer_id") == customer_id and not record.get("transition_time_minutes")
        and returned_start == ctx.start and len(actual) == len(expected))
    if exact:
        try:
            exact = all(all(a.get(k) == e.get(k) for k in ("duration_minutes", "service_variation_id", "service_variation_version", "team_member_id"))
                and int(a.get("intermission_minutes") or 0) == int(e.get("intermission_minutes") or 0)
                and set(e.get("resource_ids") or []).issubset(a.get("resource_ids") or []) for a, e in zip(actual, expected))
        except (TypeError, ValueError, AttributeError):
            exact = False
    if not exact:
        try:
            await delegate.cancel_booking(ExternalBooking(provider="square", external_id=booking_id))
        except Exception as exc:
            raise BookingProviderError(f"Partial visit requires staff reconciliation. Provider booking {booking_id} could not be cancelled.", code="PARTIAL_BOOKING_UNRESOLVED") from exc
        raise BookingProviderError("The complete visit was not confirmed; the inconsistent provider booking was cancelled.", code="VISIT_ROLLED_BACK")
    return ExternalBooking(provider="square", external_id=booking_id, external_customer_id=customer_id)
