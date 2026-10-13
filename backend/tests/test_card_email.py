from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from app.core.config import settings
from app.services.card_email import offer_secure_card_email, send_card_email
from tests.test_card_entry import _spa, _appointment, _link, _Store


@pytest.fixture
def email_case(monkeypatch):
    spa = _spa()
    contact = SimpleNamespace(id=uuid.uuid4(), tenant_id=spa.id, email="customer@example.com")
    appointment = _appointment(spa, contact.id)
    appointment.external_booking_id = "verified-booking"
    monkeypatch.setattr(settings, "CARD_EMAIL_TENANT_ID", str(spa.id))
    monkeypatch.setattr(settings, "CARD_SMTP_USER", "sender@gmail.com")
    monkeypatch.setattr(settings, "CARD_SMTP_PASSWORD", "test-only")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://cara.example.com")
    return dict(store=_Store(), spa=spa, appointment=appointment, contact=contact,
                link=_link(spa, contact.id), adapter=SimpleNamespace(supports_save_card_on_file=True),
                confirmed_email=contact.email, consent=True, sender=AsyncMock(return_value="accepted"))


@pytest.mark.asyncio
async def test_email_accepted_keeps_booking_pending_and_hides_token(email_case):
    assert await offer_secure_card_email(**email_case) == "accepted"
    token = email_case["store"].tokens[0]
    url = email_case["sender"].call_args.args[2]
    assert token.token_hash not in url
    assert len(token.token_hash) == 64
    assert email_case["appointment"].external_booking_id == "verified-booking"
    assert email_case["appointment"].card_status.value == "pending_card"


@pytest.mark.asyncio
@pytest.mark.parametrize("problem,expected", [
    ("consent", "email_confirmation_required"), ("email", "email_mismatch"),
    ("tenant", "not_configured"), ("contact", "customer_unresolved"),
    ("booking", "booking_not_confirmed"), ("http", "secure_url_required"),
])
async def test_refuse_unsafe_email(email_case, monkeypatch, problem, expected):
    if problem == "consent": email_case["consent"] = False
    if problem == "email": email_case["confirmed_email"] = "someoneelse@example.com"
    if problem == "tenant": monkeypatch.setattr(settings, "CARD_EMAIL_TENANT_ID", str(uuid.uuid4()))
    if problem == "contact": email_case["contact"].tenant_id = uuid.uuid4()
    if problem == "booking": email_case["appointment"].external_booking_id = None
    if problem == "http": monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "http://example.com")
    assert await offer_secure_card_email(**email_case) == expected
    email_case["sender"].assert_not_called()
    assert not email_case["store"].tokens


@pytest.mark.asyncio
async def test_timeout_does_not_cancel_booking_or_invalidate_possibly_delivered_link(email_case):
    email_case["sender"].side_effect = TimeoutError("sensitive transport details")
    assert await offer_secure_card_email(**email_case) == "delivery_unknown"
    assert email_case["store"].tokens[0].revoked_at is None
    assert email_case["appointment"].external_booking_id == "verified-booking"


@pytest.mark.asyncio
async def test_rejection_revokes_only_new_email_token(email_case):
    email_case["sender"].return_value = "rejected"
    assert await offer_secure_card_email(**email_case) == "rejected"
    assert email_case["store"].tokens[0].revoked_at is not None
    assert email_case["appointment"].external_booking_id == "verified-booking"


@pytest.mark.asyncio
async def test_smtp_uses_business_name_and_ssl(email_case, monkeypatch):
    transport = MagicMock()
    monkeypatch.setattr("app.services.card_email.smtplib.SMTP_SSL", transport)
    client = transport.return_value.__enter__.return_value
    client.send_message.return_value = {}
    assert await send_card_email(email_case["spa"], "customer@example.com", "https://cara.example.com/card#test") == "accepted"
    message = client.send_message.call_args.args[0]
    assert "Spa A" in str(message["From"])
    assert "sender@gmail.com" in str(message["From"])
    assert "No charge is being made now" in message.get_content()
    assert transport.call_args.kwargs["context"] is not None


@pytest.mark.asyncio
async def test_token_persistence_failure_prevents_send(email_case):
    email_case["store"].commit = AsyncMock(side_effect=RuntimeError("db unavailable"))
    with pytest.raises(RuntimeError):
        await offer_secure_card_email(**email_case)
    email_case["sender"].assert_not_called()


def test_staff_cannot_send(client, principal):
    from tests.conftest import make_user
    from app.models import UserRole
    principal.user = make_user(UserRole.SPA_STAFF, tenant_id=uuid.uuid4())
    response = client.post(f"/api/v1/appointments/{uuid.uuid4()}/card-email", json={
        "confirmed_email": "customer@example.com", "consent": True, "request_id": str(uuid.uuid4()),
    })
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_endpoint_retries_do_not_send_twice(email_case, monkeypatch):
    from app.api.v1.appointments import email_card_link, CardEmailRequest
    from app.core.tenancy import TenantScope
    from app.models import Appointment, Contact, SpaAccount
    db = MagicMock()
    appointment = email_case["appointment"]
    seen = []
    async def execute(query):
        result = MagicMock()
        sql = str(query)
        value = appointment if "FROM appointments" in sql else (
            seen[0] if seen and "FROM card_entry_tokens" in sql else
            None if "FROM card_entry_tokens" in sql else email_case["link"])
        result.scalar_one_or_none.return_value = value
        return result
    db.execute = AsyncMock(side_effect=execute)
    db.get = AsyncMock(side_effect=lambda model, key: email_case["spa"] if model is SpaAccount else email_case["contact"])
    db.add = lambda token: seen.append(token)
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    sender = AsyncMock(return_value="accepted")
    original = offer_secure_card_email
    async def deliver(**kwargs):
        return await original(**kwargs, sender=sender)
    monkeypatch.setattr("app.services.card_email.offer_secure_card_email", deliver)
    payload = CardEmailRequest(confirmed_email=email_case["contact"].email, consent=True, request_id=uuid.uuid4())
    scope = TenantScope.for_tenant(email_case["spa"].id)
    assert (await email_card_link(appointment.id, payload, scope, db))["status"] == "accepted"
    assert (await email_card_link(appointment.id, payload, scope, db))["status"] == "already_attempted"
    assert sender.await_count == 1
    assert len(seen) == 1
