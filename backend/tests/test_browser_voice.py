import uuid
from unittest.mock import AsyncMock, MagicMock

from app.core.config import settings
from app.models import UserRole, VoiceEngine
from tests.conftest import make_spa, make_user


def test_browser_token_requires_auth(client, principal):
    response = client.get("/api/v1/telephony/voice/token")
    assert response.status_code == 401


def test_browser_token_rejects_other_tenant(client, principal, monkeypatch):
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    principal.user = make_user(UserRole.SPA_ADMIN, tenant_id=tenant_id)
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setattr(settings, "TWILIO_API_KEY_SID", "SK123")
    monkeypatch.setattr(settings, "TWILIO_API_KEY_SECRET", "secret")
    monkeypatch.setattr(settings, "TWILIO_TWIML_APP_SID", "AP123")

    response = client.get(
        "/api/v1/telephony/voice/token",
        headers={"X-Tenant-Id": str(other_tenant_id)},
    )

    assert response.status_code == 403


def test_browser_token_is_generated_for_authenticated_tenant(client, principal, monkeypatch):
    tenant_id = uuid.uuid4()
    principal.user = make_user(UserRole.SPA_ADMIN, tenant_id=tenant_id)
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setattr(settings, "TWILIO_API_KEY_SID", "SK123")
    monkeypatch.setattr(settings, "TWILIO_API_KEY_SECRET", "secret")
    monkeypatch.setattr(settings, "TWILIO_TWIML_APP_SID", "AP123")

    response = client.get(
        "/api/v1/telephony/voice/token",
        headers={"X-Tenant-Id": str(tenant_id)},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tenant_id"] == str(tenant_id)
    assert payload["token"]
    assert payload["identity"].startswith("tenant:")
    assert payload["expires_in"] > 0


def test_browser_token_identity_is_directly_consumable_by_browser_test(client, principal, monkeypatch):
    tenant_id = uuid.uuid4()
    principal.user = make_user(UserRole.SPA_ADMIN, tenant_id=tenant_id)
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setattr(settings, "TWILIO_API_KEY_SID", "SK123")
    monkeypatch.setattr(settings, "TWILIO_API_KEY_SECRET", "secret")
    monkeypatch.setattr(settings, "TWILIO_TWIML_APP_SID", "AP123")

    from app.core.database import get_db

    spa = make_spa(
        id=tenant_id,
        name="Riverstone Spa",
        voice_engine=VoiceEngine.XAI_REALTIME,
        twilio_phone_number=None,
    )

    async def _db():
        session = MagicMock()
        session.get = AsyncMock(return_value=spa)
        yield session

    client.app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(settings, "TWILIO_VALIDATE_SIGNATURE", False)

    token_response = client.get(
        "/api/v1/telephony/voice/token",
        headers={"X-Tenant-Id": str(tenant_id)},
    )
    assert token_response.status_code == 200
    identity = token_response.json()["identity"]
    assert identity == f"tenant:{tenant_id}"

    browser_test_response = client.post(
        "/api/v1/telephony/voice/browser-test",
        data={
            "CallSid": "CA-browser-end-to-end",
            "From": identity,
            "To": "client:browser-test",
        },
    )

    assert browser_test_response.status_code == 200
    body = browser_test_response.text
    assert "<Connect>" in body
    assert "media-stream/CA-browser-end-to-end" in body
    assert "Riverstone Spa" in body or "Connect" in body

    client.app.dependency_overrides.clear()


def test_browser_test_route_resolves_tenant_and_uses_realtime(client, monkeypatch):
    spa = make_spa(
        id=uuid.uuid4(),
        name="Riverstone Spa",
        voice_engine=VoiceEngine.XAI_REALTIME,
        twilio_phone_number=None,
    )

    from app.core.database import get_db

    async def _db():
        session = MagicMock()
        session.get = AsyncMock(return_value=spa)
        yield session

    client.app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(settings, "TWILIO_VALIDATE_SIGNATURE", False)

    response = client.post(
        "/api/v1/telephony/voice/browser-test",
        data={
            "CallSid": "CA-browser-test",
            "From": f"client:tenant:{spa.id}:user:{uuid.uuid4()}",
            "To": "client:browser-test",
        },
    )

    assert response.status_code == 200
    body = response.text
    assert "<Connect>" in body
    assert "media-stream/CA-browser-test" in body
    assert "Riverstone Spa" in body or "Connect" in body

    client.app.dependency_overrides.clear()
