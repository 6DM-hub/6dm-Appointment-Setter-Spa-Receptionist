"""Explicit business wall time at input, aware UTC instants at boundaries."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def business_zone(name: str) -> ZoneInfo:
    if not name or (name != "UTC" and "/" not in name):
        raise ValueError("Choose an IANA business timezone, such as America/Chicago")
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Unknown IANA business timezone") from exc


def utc_instant(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp requires an explicit UTC offset")
    return value.astimezone(timezone.utc)


def localize_wall_time(value: datetime, zone) -> datetime:
    if value.tzinfo is not None:
        return utc_instant(value)
    candidates = set()
    for fold in (0, 1):
        instant = value.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if instant.astimezone(zone).replace(tzinfo=None) == value:
            candidates.add(instant)
    if len(candidates) != 1:
        raise ValueError("This local time is missing or occurs twice during daylight saving. Choose another time or specify its offset.")
    return candidates.pop().astimezone(zone)


def elapsed_end(start: datetime, minutes: int) -> datetime:
    return utc_instant(start) + timedelta(minutes=minutes)
