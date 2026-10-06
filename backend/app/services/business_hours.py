"""Business-hours evaluation for spa tenants.

`SpaAccount.business_hours` is stored as ``{"mon": [{"open": "09:00", "close":
"18:00"}], ...}`` in the tenant's local timezone. Windows are interpreted in
``SpaAccount.timezone`` and compared against UTC-aware datetimes, so a spa in
America/Los_Angeles is not accidentally judged by UTC office hours.
"""
import logging
from datetime import datetime, time, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_LABELS = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}


def resolve_timezone(name: str | None) -> tzinfo:
    """Never raises: a bad tenant timezone degrades to UTC rather than dropping
    the call. Falls back to `datetime.timezone.utc` rather than
    `ZoneInfo("UTC")`, because the reason we are here may be that no tz database
    is installed at all — in which case ZoneInfo would raise again."""
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown timezone %r; falling back to UTC", name)
        return timezone.utc


def timezone_label(tz: tzinfo) -> str:
    return getattr(tz, "key", None) or str(tz)


def _parse_time(value: str) -> time | None:
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        logger.warning("Malformed business-hours time %r; ignoring window", value)
        return None


def is_open_between(
    business_hours: dict[str, Any] | None,
    tz_name: str | None,
    start: datetime,
    end: datetime,
) -> bool:
    """True if [start, end) fits inside one of the day's opening windows.

    An empty/absent `business_hours` config means "no restriction configured" —
    a brand-new spa that has not filled in hours yet still takes bookings rather
    than silently refusing every caller.
    """
    if not business_hours:
        return True

    tz = resolve_timezone(tz_name)
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)
    duration_minutes = (end - start).total_seconds() / 60

    logger.info(
        "business_hours check: input_start=%s input_end=%s spa_tz=%s "
        "local_start=%s local_end=%s duration_min=%s",
        start.isoformat(),
        end.isoformat(),
        timezone_label(tz),
        local_start.isoformat(),
        local_end.isoformat(),
        duration_minutes,
    )

    # A window can't span midnight, so a booking that crosses days can't fit.
    if local_start.date() != local_end.date():
        return False

    windows = business_hours.get(WEEKDAY_KEYS[local_start.weekday()]) or []
    for window in windows:
        opens = _parse_time(window.get("open", ""))
        closes = _parse_time(window.get("close", ""))
        if opens is None or closes is None:
            continue
        if opens <= local_start.time() and local_end.time() <= closes:
            return True
    return False


def describe_business_hours(
    business_hours: dict[str, Any] | None, tz_name: str | None
) -> str:
    """Human-readable summary injected into the receptionist's system prompt."""
    if not business_hours:
        return "Business hours: not configured."

    lines = []
    for key in WEEKDAY_KEYS:
        windows = business_hours.get(key) or []
        if not windows:
            lines.append(f"{WEEKDAY_LABELS[key]}: closed")
            continue
        spans = ", ".join(
            f"{w.get('open', '?')} to {w.get('close', '?')}" for w in windows
        )
        lines.append(f"{WEEKDAY_LABELS[key]}: {spans}")

    tz = resolve_timezone(tz_name)
    now_local = datetime.now(timezone.utc).astimezone(tz)
    return (
        f"Business hours (all times {timezone_label(tz)}, it is currently "
        f"{now_local.strftime('%A %H:%M')} there):\n" + "\n".join(lines)
    )
