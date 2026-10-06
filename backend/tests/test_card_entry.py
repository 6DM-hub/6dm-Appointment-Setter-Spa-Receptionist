"""Phase 3 saves a card on file. It does not charge."""
from datetime import datetime, timedelta, timezone
import inspect
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.models.appointment import Appointment, AppointmentStatus, CardStatus
from app.models.card_entry_token import CardEntryToken
from app.models.external_customer_link import ExternalCustomerLink
from app.models.spa_account import BookingProvider
from app.services.booking_adapters.base import BookingProviderError
from app.services.booking_adapters.providers.square import SquareAdapter
from app.services.booking_adapters.saved_payments import SaveCardOutcome, SaveCardResult
from app.services.card_entry import (
    card_entry_url,
    card_link_block_reason,
    claim_submission,
    hash_card_token,
    new_card_token,
    offer_secure_card_sms,
    public_square_config,
    session_payload,
    sms_body,
    submit_saved_card,
    token_problem,
)
from app.services.card_entry_page import CARD_PAGE_HTML
from tests.conftest import make_spa


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _ids():
    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _spa(name="Spa A", phone="+15551110001"):
    spa = make_spa(
        name=name,
        twilio_phone_number=phone,
        booking_provider=BookingProvider.SQUARE,
        timezone="America/Chicago",
        booking_config={
            "access_token": "secret-token",
            "location_id": "LOC_A",
            "application_id": "sq0idp-public",
            "environment": "sandbox",
        },
    )
    spa.payment_policy = {"card_required": True, "collection_mode": "secure_sms_link"}
    return spa


def _appointment(spa, contact_id, *, status=CardStatus.PENDING_CARD, life=AppointmentStatus.SCHEDULED):
    appointment = Appointment(
        id=uuid.uuid4(),
        title="Massage",
        start_time=NOW,
        end_time=NOW + timedelta(hours=1),
        contact_id=contact_id,
        tenant_id=spa.id,
    )
    appointment.card_status = status
    appointment.status = life
    appointment.booking_provider = "square"
    return appointment


def _link(spa, contact_id, external_id="CUS_JOHN"):
    return ExternalCustomerLink(
        id=uuid.uuid4(),
        tenant_id=spa.id,
        contact_id=contact_id,
        provider="square",
        external_customer_id=external_id,
        last_verified_at=NOW,
    )


def _token(spa, appointment, link, *, expires=None, consumed=None, revoked=None):
    return CardEntryToken(
        id=uuid.uuid4(),
        tenant_id=spa.id,
        appointment_id=appointment.id,
        external_customer_link_id=link.id,
        token_hash=hash_card_token("raw-token-value-not-stored-anywhere"),
        idempotency_key=str(uuid.uuid4()),
        expires_at=expires or (NOW + timedelta(hours=1)),
        consumed_at=consumed,
        revoked_at=revoked,
    )


class _Store:
    def __init__(self) -> None:
        self.tokens: list[CardEntryToken] = []
        self.rolled_back = False
        self.loaded = None

    async def revoke_open(self, appointment_id, now):
        for token in self.tokens:
            if token.appointment_id == appointment_id and token.consumed_at is None and token.revoked_at is None:
                token.revoked_at = now

    async def add(self, token):
        self.tokens.append(token)

    async def commit(self):
        self.rolled_back = False

    async def rollback(self):
        self.tokens.clear()
        self.rolled_back = True

    async def find_by_hash(self, digest):
        return self.loaded

    async def claim(self, token, now):
        return claim_submission(token, now)

    async def persist(self):
        return None


class _Sender:
    def __init__(self, sid="SM1", error: Exception | None = None) -> None:
        self.sid = sid
        self.error = error
        self.calls = []

    async def __call__(self, to_number, body, from_number=None):
        self.calls.append((to_number, body, from_number))
        if self.error:
            raise self.error
        return self.sid


class _Saver:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = []

    async def save_card_on_file(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _reason(**overrides):
    values = dict(
        card_status=CardStatus.PENDING_CARD,
        card_required=True,
        collection_mode="secure_sms_link",
        guest_booking=False,
        can_save_card=True,
        external_customer_id="CUS_JOHN",
        link_matches_customer=True,
        delivery_phone="+15552220000",
        sender_phone="+15551110001",
        application_id="sq0idp-public",
        location_id="LOC_A",
    )
    values.update(overrides)
    return card_link_block_reason(**values)


def test_raw_token_is_hashed_and_not_stored_as_itself():
    raw = new_card_token()
    digest = hash_card_token(raw)
    assert digest != raw
    assert len(digest) == 64
    assert raw not in digest


@pytest.mark.parametrize(
    "status",
    [CardStatus.NOT_REQUIRED, CardStatus.NOT_SUPPORTED, CardStatus.UNKNOWN, CardStatus.CARD_CONFIRMED],
)
def test_wrong_card_status_does_not_issue_a_link(status):
    assert _reason(card_status=status) == "card_status"


def test_collection_mode_none_does_not_issue_a_link():
    assert _reason(collection_mode="none") == "collection_mode"


def test_self_booking_uses_only_the_resolved_customer():
    assert _reason() is None


def test_unresolved_guest_does_not_use_the_caller():
    assert _reason(guest_booking=True) == "guest_unresolved"


def test_missing_tenant_sender_does_not_fall_back():
    assert _reason(sender_phone=None) == "no_tenant_sender"
    assert _reason(sender_phone="  ") == "no_tenant_sender"


def test_public_config_exposes_application_and_location_only():
    public = public_square_config(
        {"access_token": "secret-token", "application_id": "sq0idp-public", "location_id": "LOC_A", "environment": "sandbox"}
    )
    assert public == {
        "application_id": "sq0idp-public",
        "location_id": "LOC_A",
        "environment": "sandbox",
    }
    assert "access_token" not in public
    assert "secret-token" not in str(public)


def test_sms_says_no_charge_and_hides_the_token_from_the_path():
    raw = "opaque-token-value"
    url = card_entry_url(raw)
    body = sms_body("Your Day Spa", url)
    assert url.endswith(f"/card#{raw}")
    assert "/card/" not in url.split("#", 1)[0]
    assert "No charge is being made now." in body
    assert "charged" not in body.lower()


@pytest.mark.asyncio
async def test_offer_stores_hash_binds_tenant_and_uses_spa_sender():
    spa = _spa()
    contact_id = uuid.uuid4()
    appointment = _appointment(spa, contact_id)
    link = _link(spa, contact_id)
    store = _Store()
    sender = _Sender()
    result = await offer_secure_card_sms(
        store=store,
        spa=spa,
        appointment=appointment,
        link=link,
        adapter=type("A", (), {"supports_save_card_on_file": True})(),
        caller_name="John Smith",
        guest_name=None,
        from_number="+15552220000",
        contact_phone=None,
        sender=sender,
    )
    assert result == "sent"
    assert len(store.tokens) == 1
    saved = store.tokens[0]
    assert saved.token_hash != sender.calls[0][1]
    assert saved.tenant_id == spa.id
    assert saved.appointment_id == appointment.id
    assert saved.external_customer_link_id == link.id
    assert sender.calls[0][0] == "+15552220000"
    assert sender.calls[0][2] == "+15551110001"
    assert "CUS_JOHN" not in sender.calls[0][1]


@pytest.mark.asyncio
async def test_guest_offer_does_not_text_the_caller():
    spa = _spa()
    contact_id = uuid.uuid4()
    appointment = _appointment(spa, contact_id)
    link = _link(spa, contact_id, "CUS_JOHN")
    store = _Store()
    sender = _Sender()
    result = await offer_secure_card_sms(
        store=store,
        spa=spa,
        appointment=appointment,
        link=link,
        adapter=type("A", (), {"supports_save_card_on_file": True})(),
        caller_name="John Smith",
        guest_name="Mary",
        from_number="+15552220000",
        contact_phone="+15552220000",
        sender=sender,
    )
    assert result == "guest_unresolved"
    assert sender.calls == []
    assert store.tokens == []


@pytest.mark.asyncio
async def test_sms_failure_keeps_the_appointment_pending_and_drops_the_token():
    spa = _spa()
    contact_id = uuid.uuid4()
    appointment = _appointment(spa, contact_id)
    store = _Store()
    result = await offer_secure_card_sms(
        store=store,
        spa=spa,
        appointment=appointment,
        link=_link(spa, contact_id),
        adapter=type("A", (), {"supports_save_card_on_file": True})(),
        caller_name="John Smith",
        guest_name=None,
        from_number="+15552220000",
        contact_phone=None,
        sender=_Sender(error=RuntimeError("twilio down")),
    )
    assert result == "sms_failed"
    assert store.rolled_back is True
    assert store.tokens == []
    assert appointment.card_status is CardStatus.PENDING_CARD


@pytest.mark.asyncio
async def test_second_link_revokes_the_first():
    spa = _spa()
    contact_id = uuid.uuid4()
    appointment = _appointment(spa, contact_id)
    link = _link(spa, contact_id)
    store = _Store()
    sender = _Sender()
    kwargs = dict(
        store=store,
        spa=spa,
        appointment=appointment,
        link=link,
        adapter=type("A", (), {"supports_save_card_on_file": True})(),
        caller_name="John Smith",
        guest_name=None,
        from_number="+15552220000",
        contact_phone=None,
        sender=sender,
    )
    await offer_secure_card_sms(**kwargs)
    await offer_secure_card_sms(**kwargs)
    active = [token for token in store.tokens if token.revoked_at is None]
    assert len(active) == 1
    assert len(store.tokens) == 2


def test_expired_consumed_cancelled_and_cross_tenant_tokens_are_rejected():
    spa = _spa()
    other = _spa(name="Spa B", phone="+15553330003")
    contact_id = uuid.uuid4()
    appointment = _appointment(spa, contact_id)
    link = _link(spa, contact_id)
    token = _token(spa, appointment, link, expires=NOW - timedelta(minutes=1))
    assert token_problem(token, appointment, link, now=NOW) == "expired"
    token.expires_at = NOW + timedelta(hours=1)
    token.consumed_at = NOW
    assert token_problem(token, appointment, link, now=NOW) == "consumed"
    token.consumed_at = None
    appointment.status = AppointmentStatus.CANCELLED
    assert token_problem(token, appointment, link, now=NOW) == "cancelled"
    appointment.status = AppointmentStatus.SCHEDULED
    token.tenant_id = other.id
    assert token_problem(token, appointment, link, now=NOW) == "tenant"


def test_a_second_claim_while_one_is_in_flight_is_busy():
    spa = _spa()
    contact_id = uuid.uuid4()
    appointment = _appointment(spa, contact_id)
    token = _token(spa, appointment, _link(spa, contact_id))
    assert claim_submission(token, NOW) == "claimed"
    assert claim_submission(token, NOW + timedelta(seconds=5)) == "busy"


@pytest.mark.asyncio
async def test_successful_save_confirms_and_consumes_only_johns_customer():
    spa = _spa()
    contact_id = uuid.uuid4()
    appointment = _appointment(spa, contact_id)
    link = _link(spa, contact_id, "CUS_JOHN")
    token = _token(spa, appointment, link)
    loaded = type("L", (), {"token": token, "appointment": appointment, "link": link, "spa": spa})()
    saver = _Saver(SaveCardResult(outcome=SaveCardOutcome.SAVED))
    store = _Store()
    outcome = await submit_saved_card(
        store=store,
        loaded=loaded,
        source_id="cnon:card-nonce",
        verification_token="verf:store",
        adapter=saver,
        now=NOW,
    )
    assert outcome == "saved"
    assert saver.calls[0]["external_customer_id"] == "CUS_JOHN"
    assert saver.calls[0]["idempotency_key"] == token.idempotency_key
    assert token.consumed_at == NOW
    assert appointment.card_status is CardStatus.CARD_CONFIRMED
    again = await submit_saved_card(
        store=store,
        loaded=loaded,
        source_id="cnon:card-nonce",
        verification_token=None,
        adapter=saver,
        now=NOW,
    )
    assert again == "already_on_file"
    assert len(saver.calls) == 1


@pytest.mark.asyncio
async def test_rejected_save_fails_the_card_status_and_keeps_the_booking():
    spa = _spa()
    contact_id = uuid.uuid4()
    appointment = _appointment(spa, contact_id)
    link = _link(spa, contact_id)
    token = _token(spa, appointment, link)
    original_key = token.idempotency_key
    loaded = type("L", (), {"token": token, "appointment": appointment, "link": link, "spa": spa})()
    saver = _Saver(SaveCardResult(outcome=SaveCardOutcome.REJECTED, error_code="CARD_DECLINED"))
    outcome = await submit_saved_card(
        store=_Store(),
        loaded=loaded,
        source_id="cnon:card-nonce",
        verification_token=None,
        adapter=saver,
        now=NOW,
    )
    assert outcome == "rejected"
    assert appointment.status is AppointmentStatus.SCHEDULED
    assert appointment.card_status is CardStatus.FAILED
    assert token.consumed_at is None
    assert token.idempotency_key != original_key


@pytest.mark.asyncio
async def test_ambiguous_save_stays_pending_and_reuses_the_idempotency_key():
    spa = _spa()
    contact_id = uuid.uuid4()
    appointment = _appointment(spa, contact_id)
    token = _token(spa, appointment, _link(spa, contact_id))
    key = token.idempotency_key
    loaded = type("L", (), {"token": token, "appointment": appointment, "link": _link(spa, contact_id), "spa": spa})()
    loaded.link = _link(spa, contact_id)
    token.external_customer_link_id = loaded.link.id
    outcome = await submit_saved_card(
        store=_Store(),
        loaded=loaded,
        source_id="cnon:card-nonce",
        verification_token=None,
        adapter=_Saver(SaveCardResult(outcome=SaveCardOutcome.AMBIGUOUS)),
        now=NOW,
    )
    assert outcome == "ambiguous"
    assert appointment.card_status is CardStatus.PENDING_CARD
    assert token.idempotency_key == key
    assert token.consumed_at is None


@pytest.mark.asyncio
async def test_cancelled_appointment_cannot_save():
    spa = _spa()
    contact_id = uuid.uuid4()
    appointment = _appointment(spa, contact_id, life=AppointmentStatus.CANCELLED)
    link = _link(spa, contact_id)
    token = _token(spa, appointment, link)
    loaded = type("L", (), {"token": token, "appointment": appointment, "link": link, "spa": spa})()
    saver = _Saver(SaveCardResult(outcome=SaveCardOutcome.SAVED))
    outcome = await submit_saved_card(
        store=_Store(),
        loaded=loaded,
        source_id="cnon:card-nonce",
        verification_token=None,
        adapter=saver,
        now=NOW,
    )
    assert outcome == "unavailable"
    assert saver.calls == []
    assert appointment.card_status is CardStatus.PENDING_CARD


@pytest.mark.asyncio
async def test_in_flight_duplicate_does_not_save_twice():
    spa = _spa()
    contact_id = uuid.uuid4()
    appointment = _appointment(spa, contact_id)
    link = _link(spa, contact_id)
    token = _token(spa, appointment, link)
    loaded = type("L", (), {"token": token, "appointment": appointment, "link": link, "spa": spa})()

    class _Busy(_Store):
        async def claim(self, token, now):
            return "busy"

    saver = _Saver(SaveCardResult(outcome=SaveCardOutcome.SAVED))
    outcome = await submit_saved_card(
        store=_Busy(),
        loaded=loaded,
        source_id="cnon:card-nonce",
        verification_token=None,
        adapter=saver,
        now=NOW,
    )
    assert outcome == "busy"
    assert saver.calls == []


def test_session_includes_known_billing_contact_only():
    spa = _spa()
    other = _spa(name="Spa B", phone="+15553330003")
    contact_id = uuid.uuid4()
    appointment = _appointment(spa, contact_id)
    link = _link(spa, contact_id)
    contact = type(
        "C",
        (),
        {
            "tenant_id": spa.id,
            "first_name": "John",
            "last_name": "Smith",
            "email": "john@example.com",
            "phone_number": "+15552220000",
        },
    )()
    loaded = type(
        "L",
        (),
        {"token": _token(spa, appointment, link), "appointment": appointment, "link": link, "spa": spa, "contact": contact},
    )()
    payload = session_payload(loaded)
    assert payload["billing_contact"] == {
        "givenName": "John",
        "familyName": "Smith",
        "email": "john@example.com",
        "phone": "+15552220000",
    }
    assert "CUS_JOHN" not in str(payload)
    assert "secret-token" not in str(payload)
    contact.tenant_id = other.id
    assert "billing_contact" not in session_payload(loaded)


def test_session_payload_has_no_secrets():
    spa = _spa()
    contact_id = uuid.uuid4()
    appointment = _appointment(spa, contact_id)
    link = _link(spa, contact_id)
    loaded = type("L", (), {"token": _token(spa, appointment, link), "appointment": appointment, "link": link, "spa": spa})()
    payload = session_payload(loaded)
    blob = str(payload)
    assert payload["application_id"] == "sq0idp-public"
    assert payload["location_id"] == "LOC_A"
    assert "secret-token" not in blob
    assert "CUS_JOHN" not in blob
    assert "tenant_id" not in payload


class _Cards:
    def __init__(self, response=None, error=None, fail_times=0) -> None:
        self.response = response or {"card": {"id": "ccof:1", "customer_id": "CUS_JOHN"}}
        self.error = error
        self.fail_times = fail_times
        self.calls = []

    async def __call__(self, method, path, *, json=None, params=None):
        self.calls.append((method, path, json or {}))
        if path == "/v2/payments" or (json or {}).get("amount_money"):
            raise AssertionError("Phase 3 must not take a payment")
        if self.fail_times:
            self.fail_times -= 1
            raise self.error or BookingProviderError("timeout", retryable=True)
        if self.error and not self.fail_times:
            raise self.error
        return self.response


def _square(spa, transport):
    adapter = SquareAdapter("Spa A", {"access_token": "secret-token", "location_id": "LOC_A"})
    adapter._request = transport
    return adapter


@pytest.mark.asyncio
async def test_square_save_posts_cards_for_the_bound_customer_only():
    spa = _spa()
    transport = _Cards()
    result = await _square(spa, transport).save_card_on_file(
        external_customer_id="CUS_JOHN",
        source_id="cnon:card-nonce",
        idempotency_key="idem-1",
        verification_token="verf:1",
    )
    assert result.outcome is SaveCardOutcome.SAVED
    method, path, payload = transport.calls[0]
    assert method == "POST"
    assert path == "/v2/cards"
    assert payload["card"]["customer_id"] == "CUS_JOHN"
    assert payload["source_id"] == "cnon:card-nonce"
    assert "amount_money" not in payload


@pytest.mark.asyncio
async def test_square_rejection_does_not_call_payments():
    transport = _Cards(error=BookingProviderError("declined", code="CARD_DECLINED"))
    result = await _square(_spa(), transport).save_card_on_file(
        external_customer_id="CUS_JOHN",
        source_id="cnon:card-nonce",
        idempotency_key="idem-1",
    )
    assert result.outcome is SaveCardOutcome.REJECTED
    assert all(call[1] != "/v2/payments" for call in transport.calls)


@pytest.mark.asyncio
async def test_square_timeout_retries_the_same_key_then_stays_ambiguous():
    transport = _Cards(fail_times=2, error=BookingProviderError("timeout", retryable=True))
    result = await _square(_spa(), transport).save_card_on_file(
        external_customer_id="CUS_JOHN",
        source_id="cnon:card-nonce",
        idempotency_key="idem-keep",
    )
    assert result.outcome is SaveCardOutcome.AMBIGUOUS
    assert [call[2]["idempotency_key"] for call in transport.calls] == ["idem-keep", "idem-keep"]


@pytest.mark.asyncio
async def test_raw_card_number_never_reaches_square():
    transport = _Cards()
    result = await _square(_spa(), transport).save_card_on_file(
        external_customer_id="CUS_JOHN",
        source_id="4242424242424242",
        idempotency_key="idem-1",
    )
    assert result.outcome is SaveCardOutcome.REJECTED
    assert transport.calls == []


@pytest.mark.asyncio
async def test_customer_mismatch_is_not_confirmed():
    transport = _Cards(response={"card": {"id": "ccof:1", "customer_id": "CUS_OTHER"}})
    result = await _square(_spa(), transport).save_card_on_file(
        external_customer_id="CUS_JOHN",
        source_id="cnon:card-nonce",
        idempotency_key="idem-1",
    )
    assert result.outcome is SaveCardOutcome.REJECTED
    assert result.error_code == "CUSTOMER_MISMATCH"


def test_phase_3_source_does_not_charge():
    from app.services import card_entry as card_entry_module
    from app.api.v1 import card_entry as card_entry_api

    blob = "\n".join(
        inspect.getsource(item)
        for item in (SquareAdapter, card_entry_module, card_entry_api)
    )
    assert "/v2/payments" not in blob
    assert "CreatePayment" not in blob
    assert "card.tokenize(verificationDetails)" in CARD_PAGE_HTML
    assert 'intent: "STORE"' in CARD_PAGE_HTML
    assert "customerInitiated: true" in CARD_PAGE_HTML
    assert "sellerKeyedIn: false" in CARD_PAGE_HTML
    assert "verifyBuyer" not in CARD_PAGE_HTML
    assert "CHARGE_AND_STORE" not in CARD_PAGE_HTML
    assert "CHARGE" not in CARD_PAGE_HTML
    assert "currencyCode" not in CARD_PAGE_HTML
    assert "amount:" not in CARD_PAGE_HTML
    assert "CreatePayment" not in CARD_PAGE_HTML
    assert "/v2/payments" not in CARD_PAGE_HTML
    assert "no-referrer" in CARD_PAGE_HTML
    assert "No charge is being made now." in CARD_PAGE_HTML


def test_invalid_token_is_a_generic_rejection():
    from app.core.database import get_db

    async def _db():
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session = MagicMock()
        session.execute = AsyncMock(return_value=result)
        yield session

    app.dependency_overrides[get_db] = _db
    try:
        client = TestClient(app)
        response = client.post("/api/v1/card-entry/session", json={"token": "not-a-real-token-value-at-all"})
        assert response.status_code == 404
        assert response.json()["detail"] == "This secure link is invalid or has expired."
        assert "tenant" not in response.json()["detail"].lower()
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        page = client.get("/card")
        assert page.status_code == 200
        assert "access_token" not in page.text
        assert "sq0idp" not in page.text
        assert page.headers["referrer-policy"] == "no-referrer"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_ttl_is_centralized():
    assert settings.CARD_ENTRY_TOKEN_TTL_SECONDS == 3600
