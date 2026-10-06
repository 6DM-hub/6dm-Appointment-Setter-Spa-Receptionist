"""Google OAuth and Calendar API operations for one spa connection."""
import asyncio
import json
import logging
from datetime import datetime
from typing import Any

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

    tz = resolve_timezone(timezone_name)
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
    body = {"summary": title, "description": description, "start": event_start, "end": event_end}
    if event_id:
        body["id"] = event_id.replace("-", "")
    try:
        service = await _service(connection)
        result = await asyncio.to_thread(
            service.events()
            .insert(calendarId=connection.selected_calendar_id, body=body, sendUpdates="none")
            .execute
        )
    except HttpError as exc:
        raise _api_error(exc) from exc
    return result["id"]


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
        await asyncio.to_thread(
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


async def delete_event(connection: GoogleCalendarConnection, event_id: str) -> None:
    if not connection.selected_calendar_id:
        return
    try:
        service = await _service(connection)
        await asyncio.to_thread(
            service.events()
            .delete(calendarId=connection.selected_calendar_id, eventId=event_id, sendUpdates="none")
            .execute
        )
    except HttpError as exc:
        if getattr(exc.resp, "status", None) != 404:
            raise _api_error(exc) from exc