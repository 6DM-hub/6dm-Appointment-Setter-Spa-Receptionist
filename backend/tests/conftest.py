"""Test harness for the RBAC / multi-tenancy layer.

These tests deliberately run without Postgres or Redis. Everything they assert
— who is refused, which scope a request resolves to, which calendar a call
books onto, what a spa's prompt contains — is decided before any query runs, so
a real database would only slow the suite down and make it skippable in CI.

Anything that genuinely needs SQL (the partial unique indexes, the
`num_nonnulls` check constraints) is exercised by applying the Alembic
migration against a real Postgres, not from here.
"""
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.redis import get_redis
from app.main import app
from app.models import BookingProvider, SpaAccount, User, UserRole, VoiceEngine


class _Principal:
    """Mutable holder so a test can switch identities on one client."""

    def __init__(self) -> None:
        self.user: User | None = None


def make_user(
    role: UserRole,
    tenant_id: uuid.UUID | None = None,
    email: str | None = None,
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email or f"{role.value}@example.test",
        hashed_password="x",
        full_name=role.value,
        role=role,
        tenant_id=tenant_id,
    )
    # Column defaults are applied on flush, which never happens here.
    user.is_active = True
    user.is_superuser = role == UserRole.SUPER_ADMIN
    user.workspace_settings = {}
    return user


def make_spa(**overrides) -> SpaAccount:
    spa = SpaAccount(
        id=overrides.pop("id", uuid.uuid4()),
        name=overrides.pop("name", "Solace Spa"),
        twilio_phone_number=overrides.pop("twilio_phone_number", "+15550000001"),
    )
    spa.grok_system_prompt = overrides.pop("grok_system_prompt", None)
    spa.business_hours = overrides.pop("business_hours", {})
    spa.services = overrides.pop("services", [])
    spa.staff = overrides.pop("staff", [])
    spa.timezone = overrides.pop("timezone", "UTC")
    spa.booking_provider = overrides.pop(
        "booking_provider", BookingProvider.GOOGLE_CALENDAR
    )
    spa.booking_config = overrides.pop("booking_config", {})
    spa.twiml_voice = overrides.pop("twiml_voice", None)
    spa.voice_engine = overrides.pop("voice_engine", VoiceEngine.TWILIO_TTS)
    spa.is_active = overrides.pop("is_active", True)
    for key, value in overrides.items():
        setattr(spa, key, value)
    return spa


@pytest.fixture
def principal() -> _Principal:
    return _Principal()


@pytest.fixture
def client(principal: _Principal) -> Iterator[TestClient]:
    """A client whose identity is whatever `principal.user` currently holds.

    `get_current_user` is overridden rather than a token being minted, so these
    tests cover the authorization rules alone and don't drift when JWT details
    change. Token issuance is covered separately in `test_auth_claims.py`.
    """
    from app.api import deps

    async def _current_user() -> User:
        if principal.user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")
        return principal.user

    async def _db():
        # Default tenant resolution is a simple tenant record so auth and browser
        # token tests can exercise the real route logic without a live database.
        async def _session_get(model, pk):
            if model is SpaAccount and principal.user is not None and principal.user.tenant_id is not None:
                return make_spa(id=principal.user.tenant_id, name="Test Spa")
            return None

        session = MagicMock(name="AsyncSession")
        session.get = AsyncMock(side_effect=_session_get)
        yield session

    async def _redis():
        redis = MagicMock(name="Redis")
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        redis.sadd = AsyncMock()
        redis.srem = AsyncMock()
        redis.expire = AsyncMock()
        redis.smembers = AsyncMock(return_value=set())
        return redis

    app.dependency_overrides[deps.get_current_user] = _current_user
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = _redis
    # No `with`: entering the context runs the lifespan, which would spend the
    # Redis connect timeout on every test session for no benefit here.
    yield TestClient(app)
    app.dependency_overrides.clear()
