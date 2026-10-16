"""Google OAuth and Calendar API operations for one spa connection."""
import asyncio
import hashlib
import hmac
import re
import json
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.core.config import settings
from app.models.google_calendar_connection import GoogleCalendarConnection
from app.services.booking_config import decrypt_config, encrypt_config

logger = logging.getLogger(__name__)

GOOGLE_CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
)



class GoogleCalendarError(RuntimeError):
    """A safe, user-facing category for a Google API failure."""

    def __init__(self, status: str, message: str = "Google Calendar request failed"):
        super().__init__(message)
        self.status = status


def _client_config() -> dict[str, Any]:
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise GoogleCalendarError("authorization_required", "Google OAuth is not configured")
    return {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def authorization_url(state: str) -> tuple[str, str]:
    flow = Flow.from_client_config(
        _client_config(), scopes=list(GOOGLE_CALENDAR_SCOPES), state=state
    )
    flow.redirect_uri = settings.GOOGLE_OAUTH_REDIRECT_URI
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    if not flow.code_verifier:
        raise GoogleCalendarError("authorization_required", "PKCE verifier was not generated")
    return url, flow.code_verifier


def _credentials_to_dict(credentials: Credentials) -> dict[str, Any]:
    return json.loads(credentials.to_json())


def _exchange_code(
    code: str, state: str, code_verifier: str
) -> tuple[dict[str, Any], str | None]:
    flow = Flow.from_client_config(
        _client_config(), scopes=list(GOOGLE_CALENDAR_SCOPES), state=state
    )
    flow.redirect_uri = settings.GOOGLE_OAUTH_REDIRECT_URI
    flow.code_verifier = code_verifier
    flow.fetch_token(code=code, code_verifier=code_verifier)
    credentials = flow.credentials
    email: str | None = None
    try:
        profile = (
            build("oauth2", "v2", credentials=credentials, cache_discovery=False)
            .userinfo()
            .get()
            .execute()
        )
        email = profile.get("email")
    except Exception:
        logger.warning("Google OAuth completed but account email lookup failed", exc_info=True)
    return _credentials_to_dict(credentials), email


async def exchange_code(
    code: str, state: str, code_verifier: str
) -> tuple[dict[str, Any], str | None]:
    return await asyncio.to_thread(_exchange_code, code, state, code_verifier)


async def _ready_credentials(connection: GoogleCalendarConnection) -> Credentials:
    info = decrypt_config(connection.credentials)
    if not info:
        raise GoogleCalendarError("authorization_required")
    try:
        credentials = Credentials.from_authorized_user_info(
            info, scopes=list(GOOGLE_CALENDAR_SCOPES)
        )
    except (ValueError, TypeError) as exc:
        raise GoogleCalendarError("authorization_required") from exc
    if credentials.expired or not credentials.valid:
        if not credentials.refresh_token:
            raise GoogleCalendarError("token_expired_or_revoked")
        try:
            await asyncio.to_thread(credentials.refresh, Request())
        except Exception as exc:
            raise GoogleCalendarError("token_expired_or_revoked") from exc
        connection.credentials = encrypt_config(_credentials_to_dict(credentials))
    return credentials


async def _service(connection: GoogleCalendarConnection):
    credentials = await _ready_credentials(connection)
    try:
        return await asyncio.to_thread(
            build, "calendar", "v3", credentials=credentials, cache_discovery=False
        )
    except Exception as exc:
        raise GoogleCalendarError("google_api_error") from exc


def _api_error(exc: HttpError) -> GoogleCalendarError:
    code = getattr(exc.resp, "status", None)
    if code == 401:
        return GoogleCalendarError("token_expired_or_revoked")
    if code == 403:
        return GoogleCalendarError("insufficient_permissions")
    if code == 404:
        return GoogleCalendarError("calendar_not_found")
    return GoogleCalendarError("google_api_error")


async def list_calendars(connection: GoogleCalendarConnection) -> list[dict[str, Any]]:
    try:
        service = await _service(connection)
        response = await asyncio.to_thread(
            service.calendarList().list(minAccessRole="reader", showDeleted=False).execute
        )
    except HttpError as exc:
        raise _api_error(exc) from exc
    return [
        {
            "id": item.get("id"),
            "summary": item.get("summary") or item.get("summaryOverride") or item.get("id"),
            "description": item.get("description"),
            "timeZone": item.get("timeZone"),
            "accessRole": item.get("accessRole"),
            "primary": bool(item.get("primary")),
        }
        for item in response.get("items", [])
        if item.get("id")
    ]


async def test_connection(connection: GoogleCalendarConnection) -> dict[str, Any]:
    if not connection.selected_calendar_id:
        raise GoogleCalendarError("calendar_not_selected")
    try:
        service = await _service(connection)
        calendar_list_entry = await asyncio.to_thread(
            service.calendarList().get(calendarId=connection.selected_calendar_id).execute
        )
    except HttpError as exc:
        raise _api_error(exc) from exc
    if calendar_list_entry.get("accessRole") not in {"writer", "owner"}:
        raise GoogleCalendarError("insufficient_permissions")
    return calendar_list_entry


def _event_times(start: datetime, end: datetime, timezone_name: str) -> tuple[dict, dict]:
    from app.services.business_hours import resolve_timezone, timezone_label

    from app.services.scheduling_time import business_zone, utc_instant
    tz = business_zone(timezone_name)
    start, end = utc_instant(start), utc_instant(end)
    return (
        {"dateTime": start.astimezone(tz).isoformat(), "timeZone": timezone_label(tz)},
        {"dateTime": end.astimezone(tz).isoformat(), "timeZone": timezone_label(tz)},
    )


async def create_event(
    connection: GoogleCalendarConnection,
    *,
    title: str,
    start: datetime,
    end: datetime,
    timezone_name: str,
    description: str,
    event_id: str | None = None,
) -> str:
    if not connection.selected_calendar_id:
        raise GoogleCalendarError("calendar_not_selected")
    event_start, event_end = _event_times(start, end, timezone_name)
    calendar_id = connection.selected_calendar_id
    body = {"summary": title, "description": description, "start": event_start, "end": event_end}
    expected = None
    if event_id:
        legacy_id = event_id.replace("-", "")
        # Preserve valid historical IDs for retries. Voice intent keys contain
        # uppercase CallSids and colons, which Google does not accept.
        body["id"] = (
            legacy_id if re.fullmatch(r"[a-v0-9]{5,1024}", legacy_id)
            else hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        )
        tenant = _connection_tenant(connection)
        if tenant:
            # Who CARA created this event for, signed so a recovery lookup can
            # tell this booking's event from any other event under the same id.
            expected = {
                CARA_TAG_VERSION_KEY: CARA_TAG_VERSION,
                CARA_TENANT_KEY: tenant,
                CARA_TAG_KEY: _event_tag(tenant, calendar_id, body["id"], event_id,
                                         event_start, event_end),
            }
            body["extendedProperties"] = {"private": dict(expected)}
    service = await _service(connection)   # nothing has been sent yet: failures are definite
    try:
        result = await asyncio.to_thread(
            service.events()
            .insert(calendarId=calendar_id, body=body, sendUpdates="none")
            .execute
        )
    except HttpError as exc:
        code = getattr(exc.resp, "status", None)
        if not _ambiguous_insert_status(code):
            raise _api_error(exc) from exc
        # 408/409/5xx: Google may hold the event (a 409 says only that the id
        # exists). Look before deciding; never insert again.
        logger.warning("Google Calendar insert outcome unclear (HTTP %s); verifying by id", code)
        return await _recover_created_event(service, calendar_id, body, expected, event_start, event_end)
    except Exception:  # noqa: BLE001 - transport errors/timeouts after sending
        # The request may have reached Google: the outcome is unknown.
        logger.warning("Google Calendar insert reply lost; verifying by id", exc_info=True)
        return await _recover_created_event(service, calendar_id, body, expected, event_start, event_end)
    if _created_event_problems(result, body, expected, event_start, event_end):
        # An HTTP success is not proof. A missing or contradictory reply goes
        # through the same exact-id verification as a lost one.
        return await _recover_created_event(service, calendar_id, body, expected, event_start, event_end)
    return result["id"]


# --------------------------------------------------------------------------- #
# Create recovery: an ambiguous insert is resolved by one exact-id read
# --------------------------------------------------------------------------- #
CARA_TAG_VERSION = "1"
CARA_TAG_VERSION_KEY = "cara_v"
CARA_TENANT_KEY = "cara_tenant"
CARA_TAG_KEY = "cara_tag"
_TAG_DOMAIN = b"cara/google-event-tag/v1"


def _connection_tenant(connection: Any) -> str | None:
    tenant = getattr(connection, "spa_id", None)
    return str(tenant) if tenant else None


def _tag_key() -> bytes:
    """Per-purpose key derived from SECRET_KEY (domain separated).

    If SECRET_KEY is rotated, tags written under the old key no longer verify:
    recovery then reports an unknown outcome for staff, never success.
    """
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), _TAG_DOMAIN, hashlib.sha256).digest()


def _utc_text(value: dict) -> str:
    instant = _event_instant(value)
    if instant is None:
        raise GoogleCalendarError("unverified_response", "Event time could not be normalized")
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_tag(tenant: str, calendar_id: str, event_id: str, reference: str,
               event_start: dict, event_end: dict) -> str:
    """HMAC over the canonical identity of the event CARA asked Google to create.

    It authenticates these fields; it does not stop a calendar editor from
    copying a whole signed event, so the expected identity always comes from
    CARA's own request, never from the event alone.
    """
    canonical = "|".join(
        ("v" + CARA_TAG_VERSION, tenant, calendar_id, event_id, reference,
         _utc_text(event_start), _utc_text(event_end))
    )
    return hmac.new(_tag_key(), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _ambiguous_insert_status(code: Any) -> bool:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return True
    return code in {408, 409} or code >= 500


def _created_event_problems(result: Any, body: dict, expected: dict | None,
                            event_start: dict, event_end: dict) -> list[str]:
    """Everything that stops ``result`` proving it is the event we created."""
    if not isinstance(result, dict):
        return ["no event returned"]
    problems = []
    if not result.get("id"):
        problems.append("no event id")
    elif body.get("id") and result.get("id") != body["id"]:
        problems.append("a different event id")
    if result.get("status") not in _ACTIVE_EVENT_STATUSES:
        problems.append("the event is not active")
    for label, returned, requested in (("start", result.get("start"), event_start),
                                       ("end", result.get("end"), event_end)):
        instant = _event_instant(returned)
        if instant is None:
            problems.append(f"the {label} time is missing or unreadable")
        elif instant != _event_instant(requested):
            problems.append(f"the {label} time differs")
    if expected is not None:
        private = ((result.get("extendedProperties") or {}).get("private") or {})
        if not isinstance(private, dict):
            private = {}
        if private.get(CARA_TAG_VERSION_KEY) != expected[CARA_TAG_VERSION_KEY]:
            problems.append("missing or unknown CARA tag version")
        if private.get(CARA_TENANT_KEY) != expected[CARA_TENANT_KEY]:
            problems.append("a different or missing business")
        tag = private.get(CARA_TAG_KEY)
        if not isinstance(tag, str) or not hmac.compare_digest(tag, expected[CARA_TAG_KEY]):
            problems.append("the CARA tag does not verify")
    return problems


async def _recover_created_event(service: Any, calendar_id: str, body: dict, expected: dict | None,
                                 event_start: dict, event_end: dict) -> str:
    """One read of the exact expected event id; success only on full proof.

    No list search, no second insert and no new id. Anything short of a full
    match (missing event, unreadable reply, other times, cancelled, missing or
    invalid tag, lookup failure) is an unknown outcome for staff to resolve.
    """
    event_id = body.get("id")
    if not event_id or expected is None:
        raise GoogleCalendarError("unverified_response", "Google Calendar did not confirm the new event")
    try:
        found = await asyncio.to_thread(
            service.events().get(calendarId=calendar_id, eventId=event_id).execute
        )
    except Exception:  # noqa: BLE001 - 404, transport, anything: still unknown
        logger.warning("Google Calendar recovery lookup failed for %s", event_id, exc_info=True)
        raise GoogleCalendarError("unverified_response", "Google Calendar did not confirm the new event")
    problems = _created_event_problems(found, body, expected, event_start, event_end)
    if problems:
        logger.warning("Google Calendar recovery did not prove event %s: %s", event_id, "; ".join(problems))
        raise GoogleCalendarError("unverified_response", "Google Calendar did not confirm the new event")
    logger.info("Google Calendar create recovered by verified lookup: %s", event_id)
    return found["id"]


async def freebusy(
    connection: GoogleCalendarConnection,
    *,
    calendar_id: str,
    start: datetime,
    end: datetime,
    timezone_name: str,
) -> bool:
    if not connection.selected_calendar_id:
        raise GoogleCalendarError("calendar_not_selected")
    event_start, event_end = _event_times(start, end, timezone_name)
    try:
        service = await _service(connection)
        response = await asyncio.to_thread(
            service.freebusy()
            .query(
                body={
                    "timeMin": event_start["dateTime"],
                    "timeMax": event_end["dateTime"],
                    "items": [{"id": calendar_id}],
                }
            )
            .execute
        )
    except HttpError as exc:
        raise _api_error(exc) from exc
    return bool(response.get("calendars", {}).get(calendar_id, {}).get("busy"))


# Google event statuses that mean "this event is on the calendar". Anything
# else (above all "cancelled") is not a successful move.
_ACTIVE_EVENT_STATUSES = {"confirmed", "tentative"}


def _event_instant(value: Any) -> datetime | None:
    """The absolute instant of a Google event start/end object, or None.

    Compares instants, never wall-clock text, so a reply that spells the same
    moment in another UTC offset or time zone still matches. A missing field, an
    all-day ``date``, or a time that cannot be placed on the timeline returns
    None: the caller treats that as unverifiable.
    """
    if not isinstance(value, dict):
        return None
    raw = value.get("dateTime")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            zone_name = value.get("timeZone")
            if not isinstance(zone_name, str):
                return None
            parsed = parsed.replace(tzinfo=ZoneInfo(zone_name))
        return parsed.astimezone(timezone.utc)
    except (ValueError, ZoneInfoNotFoundError):
        return None


def _verify_patched_event(result: Any, event_id: str, event_start: dict, event_end: dict) -> None:
    """Raise unless Google's reply proves the requested move.

    An HTTP success is not proof. The reply must be the event we patched, still
    active, and now holding exactly the requested start and end instants.
    Otherwise the calendar may or may not hold the change, so the caller must
    treat the outcome as unknown (and block retries) rather than as applied.
    """
    problems = []
    if not isinstance(result, dict):
        problems.append("no event returned")
    else:
        if result.get("id") != event_id:
            problems.append("a different event was returned")
        if result.get("status") not in _ACTIVE_EVENT_STATUSES:
            problems.append("the event is not active")
        for label, returned, requested in (
            ("start", result.get("start"), event_start),
            ("end", result.get("end"), event_end),
        ):
            instant = _event_instant(returned)
            if instant is None:
                problems.append(f"the {label} time is missing or unreadable")
            elif instant != _event_instant(requested):
                problems.append(f"the {label} time is not the requested time")
    if problems:
        # Log what failed, never the event's contents (customer details).
        logger.warning("Google Calendar reply did not prove the move of %s: %s", event_id, "; ".join(problems))
        raise GoogleCalendarError(
            "unverified_response",
            "Google Calendar did not confirm the requested change",
        )


async def update_event(
    connection: GoogleCalendarConnection,
    event_id: str,
    *,
    start: datetime,
    end: datetime,
    timezone_name: str,
) -> None:
    if not connection.selected_calendar_id:
        raise GoogleCalendarError("calendar_not_selected")
    event_start, event_end = _event_times(start, end, timezone_name)
    try:
        service = await _service(connection)
        result = await asyncio.to_thread(
            service.events()
            .patch(
                calendarId=connection.selected_calendar_id,
                eventId=event_id,
                body={"start": event_start, "end": event_end},
                sendUpdates="none",
            )
            .execute
        )
    except HttpError as exc:
        raise _api_error(exc) from exc
    # Success is what Google's reply proves, not the absence of an exception.
    _verify_patched_event(result, event_id, event_start, event_end)


async def delete_event(connection: GoogleCalendarConnection, event_id: str) -> None:
    """Delete an event from the selected calendar, or raise.

    Never reports success without Google confirming this delete: no selected
    calendar, or an event that calendar does not hold (404 -- e.g. the tenant
    switched calendars since it was created), is an error for the caller to
    surface, not a silent no-op.
    """
    if not connection.selected_calendar_id:
        raise GoogleCalendarError("calendar_not_selected")
    try:
        service = await _service(connection)
        await asyncio.to_thread(
            service.events()
            .delete(calendarId=connection.selected_calendar_id, eventId=event_id, sendUpdates="none")
            .execute
        )
    except HttpError as exc:
        if getattr(exc.resp, "status", None) == 404:
            raise GoogleCalendarError("event_not_found") from exc
        raise _api_error(exc) from exc