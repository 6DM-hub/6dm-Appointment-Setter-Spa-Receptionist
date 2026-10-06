import uuid
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1.spa_accounts import (
    _connection_read,
    connect_google_calendar,
    google_oauth_callback,
)
from app.core.config import settings
from app.models import SpaAccount, User, UserRole
from app.models.google_calendar_connection import GoogleCalendarConnection
from app.services import google_calendar
from app.services.booking_adapters.base import BookingContext, ExternalBooking
from app.services.booking_adapters.google_calendar import GoogleCalendarAdapter
from app.services.booking_config import decrypt_config, encrypt_config


def make_connection(calendar_id: str | None = "spa-calendar") -> GoogleCalendarConnection:
    return GoogleCalendarConnection(
        id=uuid.uuid4(),
        spa_id=uuid.uuid4(),
        selected_calendar_id=calendar_id,
        credentials=encrypt_config({"access_token": "secret", "refresh_token": "refresh"}),
    )


def test_oauth_url_uses_backend_client_and_offline_access(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "client-secret")

    url, verifier = google_calendar.authorization_url("csrf-state")

    assert "client_id=client-id" in url
    assert "state=csrf-state" in url
    assert "access_type=offline" in url
    assert "calendar" in url
    assert verifier


@pytest.mark.asyncio
async def test_invalid_oauth_state_is_rejected_without_touching_a_tenant():
    class Redis:
        async def getdel(self, _key):
            return None

    response = await google_oauth_callback(
        code="code", state="invalid", db=object(), redis=Redis()
    )

    assert "invalid_state" in response.headers["location"]


@pytest.mark.asyncio
async def test_oauth_callback_stores_credentials_for_state_tenant(monkeypatch):
    spa = SpaAccount(id=uuid.uuid4(), name="Tenant Spa")
    user = User(
        id=uuid.uuid4(),
        email="owner@example.test",
        hashed_password="x",
        full_name="Owner",
        role=UserRole.SPA_ADMIN,
        tenant_id=spa.id,
    )
    user.is_active = True

    class Result:
        def scalar_one_or_none(self):
            return None

    class DB:
        def __init__(self):
            self.added = None

        async def get(self, model, key):
            return user if model is User else spa

        async def execute(self, _query):
            return Result()

        def add(self, value):
            self.added = value

        async def commit(self):
            return None

    class Redis:
        async def getdel(self, _key):
            return json.dumps({
                "spa_id": str(spa.id),
                "user_id": str(user.id),
                "code_verifier": "stored-verifier",
            })

    credentials = {"access_token": "access", "refresh_token": "refresh", "scopes": ["calendar"]}
    monkeypatch.setattr(google_calendar, "exchange_code", AsyncMock(return_value=(credentials, user.email)))
    exchange = AsyncMock(return_value=(credentials, user.email))
    monkeypatch.setattr("app.api.v1.spa_accounts.exchange_code", exchange)

    response = await google_oauth_callback(
        code="code", state="valid", db=DB(), redis=Redis()
    )

    assert response.status_code == 303
    assert "google_calendar=connected" in response.headers["location"]
    exchange.assert_awaited_once_with("code", "valid", "stored-verifier")


@pytest.mark.asyncio
async def test_missing_pkce_verifier_returns_controlled_error():
    spa_id = uuid.uuid4()
    user_id = uuid.uuid4()

    class Redis:
        async def getdel(self, _key):
            return json.dumps({"spa_id": str(spa_id), "user_id": str(user_id)})

    user = User(
        id=user_id,
        email="owner@example.test",
        hashed_password="x",
        full_name="Owner",
        role=UserRole.SPA_ADMIN,
        tenant_id=spa_id,
    )
    user.is_active = True
    spa = SpaAccount(id=spa_id, name="Tenant Spa")

    class DB:
        async def get(self, model, _key):
            return user if model is User else spa

    response = await google_oauth_callback(
        code="code", state="valid", db=DB(), redis=Redis()
    )

    assert "missing_verifier" in response.headers["location"]


@pytest.mark.asyncio
async def test_connect_persists_generated_verifier_without_returning_it(monkeypatch):
    spa = SpaAccount(id=uuid.uuid4(), name="Tenant Spa")
    user = User(
        id=uuid.uuid4(),
        email="owner@example.test",
        hashed_password="x",
        full_name="Owner",
        role=UserRole.SPA_ADMIN,
        tenant_id=spa.id,
    )
    user.is_active = True

    class DB:
        async def get(self, _model, _key):
            return spa

    stored = {}

    class Redis:
        async def setex(self, key, ttl, value):
            stored.update(key=key, ttl=ttl, value=value)

    monkeypatch.setattr(
        "app.api.v1.spa_accounts.authorization_url",
        lambda _state: ("https://accounts.google.test/auth", "exact-verifier"),
    )
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "client-secret")

    result = await connect_google_calendar(spa.id, user, DB(), Redis())

    payload = json.loads(stored["value"])
    assert result == {"authorization_url": "https://accounts.google.test/auth"}
    assert payload["code_verifier"] == "exact-verifier"
    assert payload["spa_id"] == str(spa.id)
    assert payload["user_id"] == str(user.id)
    assert "exact-verifier" not in result["authorization_url"]


@pytest.mark.asyncio
async def test_oauth_state_is_one_time_use(monkeypatch):
    spa = SpaAccount(id=uuid.uuid4(), name="Tenant Spa")
    user = User(
        id=uuid.uuid4(),
        email="owner@example.test",
        hashed_password="x",
        full_name="Owner",
        role=UserRole.SPA_ADMIN,
        tenant_id=spa.id,
    )
    user.is_active = True
    state_record = json.dumps({
        "spa_id": str(spa.id),
        "user_id": str(user.id),
        "code_verifier": "one-time-verifier",
    })

    class Redis:
        used = False

        async def getdel(self, _key):
            if self.used:
                return None
            self.used = True
            return state_record

    class Result:
        def scalar_one_or_none(self):
            return None

    class DB:
        async def get(self, model, _key):
            return user if model is User else spa

        async def execute(self, _query):
            return Result()

        def add(self, _value):
            pass

        async def commit(self):
            pass

    exchange = AsyncMock(return_value=({"access_token": "access"}, user.email))
    monkeypatch.setattr("app.api.v1.spa_accounts.exchange_code", exchange)
    redis = Redis()

    first = await google_oauth_callback(code="code", state="same", db=DB(), redis=redis)
    second = await google_oauth_callback(code="code", state="same", db=DB(), redis=redis)

    assert "google_calendar=connected" in first.headers["location"]
    assert "invalid_state" in second.headers["location"]
    exchange.assert_awaited_once()


def test_connection_read_never_returns_credentials():
    connection = make_connection()

    result = _connection_read(connection)

    assert result["selected_calendar_id"] == "spa-calendar"
    assert "credentials" not in result
    assert "secret" not in str(result)
    assert decrypt_config(connection.credentials)["refresh_token"] == "refresh"


@pytest.mark.asyncio
async def test_google_connection_test_checks_selected_calendar(monkeypatch):
    connection = make_connection()

    class Call:
        def execute(self):
            return {"id": "spa-calendar", "accessRole": "writer"}

    service = SimpleNamespace(
        calendarList=lambda: SimpleNamespace(get=lambda **_kwargs: Call())
    )
    monkeypatch.setattr(google_calendar, "_service", AsyncMock(return_value=service))

    calendar = await google_calendar.test_connection(connection)

    assert calendar["accessRole"] == "writer"


@pytest.mark.asyncio
async def test_missing_calendar_is_reported_before_google_request():
    with pytest.raises(google_calendar.GoogleCalendarError) as error:
        await google_calendar.test_connection(make_connection(None))

    assert error.value.status == "calendar_not_selected"


@pytest.mark.asyncio
async def test_expired_access_token_is_refreshed_and_reencrypted(monkeypatch):
    connection = make_connection()

    class Credentials:
        expired = True
        valid = False
        refresh_token = "refresh"

        def refresh(self, _request):
            self.expired = False
            self.valid = True

        def to_json(self):
            return json.dumps({"token": "new-access", "refresh_token": "refresh"})

    monkeypatch.setattr(google_calendar, "Credentials", SimpleNamespace(
        from_authorized_user_info=lambda *_args, **_kwargs: Credentials()
    ))

    await google_calendar._ready_credentials(connection)

    assert decrypt_config(connection.credentials)["token"] == "new-access"


def test_google_401_is_classified_as_revoked_token():
    error = google_calendar._api_error(SimpleNamespace(resp=SimpleNamespace(status=401)))

    assert error.status == "token_expired_or_revoked"


@pytest.mark.asyncio
async def test_google_adapter_creates_updates_and_deletes_event(monkeypatch):
    connection = make_connection()
    adapter = GoogleCalendarAdapter(
        calendar_id="spa-calendar",
        connection=connection,
        timezone_name="America/Chicago",
    )
    create = AsyncMock(return_value="event-1")
    update = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr("app.services.booking_adapters.google_calendar.create_event", create)
    monkeypatch.setattr("app.services.booking_adapters.google_calendar.update_event", update)
    monkeypatch.setattr("app.services.booking_adapters.google_calendar.delete_event", delete)
    context = BookingContext(
        start=datetime(2026, 9, 2, 14, tzinfo=timezone.utc),
        end=datetime(2026, 9, 2, 15, tzinfo=timezone.utc),
        title="Facial",
        customer_phone="+15550001",
        customer_name="Alex Guest",
        service_description="Facial",
        booking_reference=str(uuid.uuid4()),
    )

    created = await adapter.create_booking(context)
    moved = await adapter.move_booking(created, context.start, context.end)
    await adapter.cancel_booking(created)

    assert created == ExternalBooking(provider="google_calendar", external_id="event-1")
    assert moved == created
    create.assert_awaited_once()
    assert create.await_args.kwargs["event_id"] == context.booking_reference
    update.assert_awaited_once()
    delete.assert_awaited_once_with(connection, "event-1")


def test_event_times_use_spa_central_timezone():
    start, end = google_calendar._event_times(
        datetime(2026, 9, 2, 14, tzinfo=timezone.utc),
        datetime(2026, 9, 2, 15, tzinfo=timezone.utc),
        "America/Chicago",
    )

    assert start["dateTime"].endswith("-05:00")
    assert end["dateTime"].startswith("2026-09-02T10:00")
