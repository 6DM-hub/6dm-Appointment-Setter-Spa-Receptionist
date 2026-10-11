"""Returning customer privacy, profile persistence and visit feedback tests.

No Square/Twilio network calls or production database required.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.models import Contact
from app.services.call_state import CallSession
from app.services.booking_adapters.customers import CustomerLookupOutcome, CustomerLookupResult, ProviderCustomer
from app.services import customer_memory as cm


def _session(*, tenant_id=None, phone="+15551234567"):
    return CallSession(
        call_sid="cara-test:profile", direction="inbound",
        from_number=phone, to_number="+15550000000",
        tenant_id=str(tenant_id or uuid.uuid4()), timezone="UTC",
    )


def _contact(session, *, name="Jane Doe"):
    first, last = name.split(" ", 1)
    return Contact(
        id=uuid.uuid4(), tenant_id=uuid.UUID(session.tenant_id),
        first_name=first, last_name=last,
        phone_number=session.customer_phone, extra_metadata={},
    )


def _result(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value)


def test_full_name_is_required_and_phone_is_not_identity():
    session = _session()
    assert not cm._claimed_name_grounded(session, "Jane Doe")
    session.add_turn("user", "I'd like to see my usual treatment.")
    assert not cm._claimed_name_grounded(session, "Jane Doe")
    session.add_turn("user", "My name is Jane Doe.")
    assert cm._claimed_name_grounded(session, "Jane Doe")
    assert not cm._claimed_name_grounded(session, "Janet Doe")
    assert not cm._full_name_match("Jane Doe", "Jane")
    assert cm._full_name_match("Jane Doe", "JANE  DOE")


@pytest.mark.asyncio
async def test_name_required_before_database_or_provider_lookup():
    session = _session()
    db = SimpleNamespace(execute=AsyncMock())
    r = await cm.customer_profile_action(db, session, caller_name="Jane Doe")
    assert r["status"] == "name_required"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_tenant_scope_used_for_contact_and_square_resolution(monkeypatch):
    session = _session()
    session.add_turn("user", "My name is Jane Doe.")
    statements = []
    async def execute(statement):
        statements.append(statement)
        return _result(None)
    db = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    _, _, status = await cm._resolve_profile(db, session, "Jane Doe")
    assert status == "not_found"
    assert len(statements) == 2
    # Contact read is scoped to exactly this spa, not the sales workspace.
    sql = str(statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert session.tenant_id.replace("-", "") in sql.replace("-", "")
    assert "contacts.tenant_id" in sql
    assert "contacts.phone_number" in sql
    # External provider is only looked up via this tenant's SpaAccount row.
    spa_sql = str(statements[1].compile(compile_kwargs={"literal_binds": True}))
    assert session.tenant_id.replace("-", "") in spa_sql.replace("-", "")


@pytest.mark.asyncio
async def test_square_customer_matched_only_after_name_and_provider_lookup(monkeypatch):
    session = _session()
    spa = SimpleNamespace(id=uuid.UUID(session.tenant_id))
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        _result(None), _result(spa),
    ]))
    class SquareStub:
        supports_customer_lookup = True
        find_customers_by_phone = AsyncMock(return_value=CustomerLookupResult(
            outcome=CustomerLookupOutcome.AMBIGUOUS,
            candidates=(
                ProviderCustomer("sq_jane", display_name="Jane Doe"),
                ProviderCustomer("sq_john", display_name="John Doe"),
            ),
        ))
    monkeypatch.setattr(cm, "SpaBookingAdapter", lambda spa: SquareStub())
    contact, name, status = await cm._resolve_profile(db, session, "Jane Doe")
    assert contact is None
    assert name == "Jane Doe"
    assert status == "verified_square"


@pytest.mark.asyncio
async def test_remember_only_explicit_non_sensitive_caller_spoken_preference(monkeypatch):
    session = _session()
    contact = _contact(session)
    session.add_turn("user", "My name is Jane Doe.")
    monkeypatch.setattr(cm, "_resolve_profile", AsyncMock(return_value=(contact, "Jane Doe", "verified")))
    db = SimpleNamespace(commit=AsyncMock())
    found = await cm.customer_profile_action(db, session, caller_name="Jane Doe")
    assert found["status"] == "found"
    assert found["preferences"] == {}
    session.add_turn("user", "I like Swedish massage.")
    no_consent = await cm.customer_profile_action(
        db, session, action="remember", preference_key="favorite_service",
        preference_value="Swedish massage",
    )
    assert no_consent["status"] == "consent_required"
    session.add_turn("user", "Please remember Swedish massage as my usual service.")
    saved = await cm.customer_profile_action(
        db, session, action="remember", preference_key="favorite_service",
        preference_value="Swedish massage",
    )
    assert saved["status"] == "saved"
    assert cm._saved_preferences(contact) == {"favorite_service": "Swedish massage"}
    session.add_turn("user", "Remember I have acne and pain in my neck.")
    unsafe = await cm.customer_profile_action(
        db, session, action="remember", preference_key="treatment_style",
        preference_value="acne and pain in my neck",
    )
    assert unsafe["status"] == "unsupported_preference"
    assert "treatment_style" not in cm._saved_preferences(contact)


@pytest.mark.asyncio
async def test_feedback_is_optional_and_cannot_save_before_an_answer(monkeypatch):
    session = _session()
    contact = _contact(session)
    visit = {
        "appointment_id": "verified-appointment-id",
        "service": "European Facial",
        "date": "2026-09-23",
    }
    session.add_turn("user", "My name is Jane Doe.")
    monkeypatch.setattr(cm, "_resolve_profile", AsyncMock(return_value=(contact, "Jane Doe", "verified")))
    monkeypatch.setattr(cm, "_latest_previous_visit", AsyncMock(return_value=visit))
    db = SimpleNamespace(commit=AsyncMock())
    invite = await cm.customer_profile_action(db, session, action="feedback_invite", caller_name="Jane Doe")
    assert invite["status"] == "feedback_invited"
    assert invite["last_verified_service"] == "European Facial"
    premature = await cm.customer_profile_action(
        db, session, action="feedback_save", feedback_text="It was wonderful!",
    )
    assert premature["status"] == "awaiting_answer"
    assert not contact.extra_metadata
    session.add_turn("user", "It was wonderful! The staff were very helpful.")
    saved = await cm.customer_profile_action(
        db, session, action="feedback_save", feedback_text="It was wonderful!",
    )
    assert saved["status"] == "feedback_saved"
    entries = contact.extra_metadata[cm.MEMORY_KEY]["visit_feedback"]
    assert len(entries) == 1
    assert entries[0]["verified_visit"] is True
    assert entries[0]["appointment_id"] == "verified-appointment-id"
    assert entries[0]["feedback"] == "It was wonderful!"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_feedback_decline_no_records(monkeypatch):
    session = _session()
    contact = _contact(session)
    session.add_turn("user", "My name is Jane Doe.")
    monkeypatch.setattr(cm, "_resolve_profile", AsyncMock(return_value=(contact, "Jane Doe", "verified")))
    monkeypatch.setattr(cm, "_latest_previous_visit", AsyncMock(return_value=None))
    db = SimpleNamespace(commit=AsyncMock())
    invite = await cm.customer_profile_action(db, session, action="feedback_invite", caller_name="Jane Doe")
    assert "If you've visited us before" in invite["message"]
    session.add_turn("user", "No thanks.")
    declined = await cm.customer_profile_action(
        db, session, action="feedback_save", feedback_text="No thanks."
    )
    assert declined["status"] == "declined"
    assert not contact.extra_metadata
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_feedback_not_saved_when_sensitive_or_fabricated(monkeypatch):
    session = _session()
    contact = _contact(session)
    session.add_turn("user", "Jane Doe")
    monkeypatch.setattr(cm, "_resolve_profile", AsyncMock(return_value=(contact, "Jane Doe", "verified")))
    monkeypatch.setattr(cm, "_latest_previous_visit", AsyncMock(return_value=None))
    db = SimpleNamespace(commit=AsyncMock())
    await cm.customer_profile_action(db, session, action="feedback_invite", caller_name="Jane Doe")
    session.add_turn("user", "I had a rash after the treatment.")
    result = await cm.customer_profile_action(
        db, session, action="feedback_save", feedback_text="I had a rash after the treatment."
    )
    assert result["status"] == "not_saved"
    assert not contact.extra_metadata


@pytest.mark.asyncio
async def test_forget_saved_preferences_and_feedback(monkeypatch):
    session = _session()
    contact = _contact(session)
    contact.extra_metadata = {
        cm.MEMORY_KEY: {
            "preferences": {"favorite_service": "European Facial"},
            "visit_feedback": [{"feedback": "Lovely staff"}],
        }
    }
    session.add_turn("user", "Jane Doe")
    monkeypatch.setattr(cm, "_resolve_profile", AsyncMock(return_value=(contact, "Jane Doe", "verified")))
    db = SimpleNamespace(commit=AsyncMock())
    await cm.customer_profile_action(db, session, caller_name="Jane Doe")
    session.add_turn("user", "Please forget all my saved preferences and feedback.")
    result = await cm.customer_profile_action(
        db, session, action="forget", preference_key="all"
    )
    assert result["status"] == "forgotten"
    assert contact.extra_metadata[cm.MEMORY_KEY]["preferences"] == {}
    assert "visit_feedback" not in contact.extra_metadata[cm.MEMORY_KEY]
