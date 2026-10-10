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
       ![ÛˆÚ[œË›İHÜšYÚ[˜[ˆÈKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKHÂ˜\Ş[˜ÈYˆ\İØ—ØÛÜœ™XİYÙ]WÜ™\ÛÛ™\×İ×İWÛ™]×Ù]WÛ›İİWÛÜšYÚ[˜[
ˆ›ÚXÙWÜÙ\ÜÚ[Û‹]˜Z[Xš[]WÜİX‹[ÛšÙ^\]ÚŠN‚ˆÙ[ˆ\İÙXİHH×B‚ˆ\Ş[˜ÈYˆØ\\™J^[ØY
N‚ˆÙ[˜\[™
^[ØY
B‚ˆ[ÛšÙ^\]ÚœÙ]]Š›ÚXÙWÜÙ\ÜÚ[Û‹—ÜÙ[™‹Ø\\™JB‚ˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹“ØİØ™\ˆ\İˆŠBˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹XİX[HXZÙH]ØİØ™\ˆ›™ˆŠBˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹ŒÈKˆŠB‚ˆÈH[Ù[ÛÜœ™XİH›ÜÜÙ\ÈØİØ™\ˆ›™
Ú]H™X[[Ù[Ûİ[ˆÈÛÛXš[™Hœ›ÛHHØ[\‰ÜÈİÛˆ[Üİ™XÙ[ÛÜœ™Xİ[ÛŠH8 %]\İ™BˆÈXØÙ\Y‚ˆ]ØZ]Ü›Ø™J›ÚXÙWÜÙ\ÜÚ[Û‹Ù[œ™\ÜLH‹˜Ø[LH‹ŒŒ‹LLL•MNŒŒŠBˆ\ÜÙ\Û\İÜİ]\ÊÙ[
HOH˜]˜Z[X›H‚ˆ\ÜÙ\]˜Z[Xš[]WÜİXˆOHÈŒŒ‹LLL•MNŒŒ—B‚‚˜\Ş[˜ÈYˆ\İØ—İWÜİ\\œÙYYÛÜšYÚ[˜[Ù]WÚ\×Û›×ÛÛ™Ù\—ÙÜ›İ[™Y
ˆ›ÚXÙWÜÙ\ÜÚ[Û‹]˜Z[Xš[]WÜİX‹[ÛšÙ^\]ÚŠN‚ˆˆˆ”Ø[YHÛÛ™\œØ][Ûˆ\ÈX›İ™K]H[Ù[
Ü›Û™ÛJHšY\ÈÈÛÛXš[™BˆŒÈHˆÚ]HÕSHØİØ™\ˆ\İ[œİXYÙˆHÛÜœ™Xİ[Û‹ˆ]\İ™Bˆ™Y\ÙY›İÚ[[H›ÛÚÙYÛˆHÜ›Û™È^Kˆˆˆ‚ˆÙ[ˆ\İÙXİHH×B‚ˆ\Ş[˜ÈYˆØ\\™J^[ØY
N‚ˆÙ[˜\[™
^[ØY
B‚ˆ[ÛšÙ^\]ÚœÙ]]Š›ÚXÙWÜÙ\ÜÚ[Û‹—ÜÙ[™‹Ø\\™JB‚ˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹“ØİØ™\ˆ\İˆŠBˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹XİX[HXZÙH]ØİØ™\ˆ›™ˆŠBˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹ŒÈKˆŠB‚ˆ]ØZ]Ü›Ø™J›ÚXÙWÜÙ\ÜÚ[Û‹Ù[œ™\ÜLH‹˜Ø[LH‹ŒŒ‹LLLUMNŒŒŠBˆ\ÜÙ\Û\İÜİ]\ÊÙ[
HOH[™Ü›İ[™Yİ[YH‚ˆ\ÜÙ\]˜Z[Xš[]WÜİXˆOH×B‚‚ˆÈKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKHÂˆÈËˆØ[\ˆØ[˜Ù[ÈH]Hİ]šYÚÈ]]\İ›İ™H™\İ\œ™XİYˆÈKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKHÂ˜\Ş[˜ÈYˆ\İØ×ØØ[˜Ù[YÙ]WÚ\×Û›İÜ™\İ\œ™XİYÙ›Ü—ØWÛ]\—İ[YWÛÛ›Wİ\›Šˆ›ÚXÙWÜÙ\ÜÚ[Û‹]˜Z[Xš[]WÜİX‹[ÛšÙ^\]ÚŠN‚ˆÙ[ˆ\İÙXİHH×B‚ˆ\Ş[˜ÈYˆØ\\™J^[ØY
N‚ˆÙ[˜\[™
^[ØY
B‚ˆ[ÛšÙ^\]ÚœÙ]]Š›ÚXÙWÜÙ\ÜÚ[Û‹—ÜÙ[™‹Ø\\™JB‚ˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹“ØİØ™\ˆ\İˆŠBˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹“™]™\ˆZ[™]]KˆŠBˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹ŒÈKˆŠB‚ˆ]ØZ]Ü›Ø™J›ÚXÙWÜÙ\ÜÚ[Û‹Ù[œ™\ÜLH‹˜Ø[LH‹ŒŒ‹LLLUMNŒŒŠB‚ˆ\ÜÙ\Û\İÜİ]\ÊÙ[
HOH[™Ü›İ[™Yİ[YH‚ˆ\ÜÙ\]˜Z[Xš[]WÜİXˆOH×B‚‚ˆÈKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKHÂˆÈˆH]HY[[Û™YÛÛ™\œØ][Û˜[H
›İ\È\ÙˆH›ÛÚÚ[™È™\]Y\İ
BˆÈKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKHÂ˜\Ş[˜ÈYˆ\İÙØWØÛÛ™\œØ][Û˜[Ù]WÛY[[Û—ØØ[››İØ™WÙ\İ[™İZ\ÚYÙœ›ÛWØWØ›ÛÚÚ[™×ÛÛ™Jˆ›ÚXÙWÜÙ\ÜÚ[Û‹]˜Z[Xš[]WÜİX‹[ÛšÙ^\]ÚŠN‚ˆˆˆ‘Øİ[Y[ÈH™X[Û›İÛˆ[Z]][Ûˆ˜]\ˆ[ˆ\ÜÙ\[™ÈH\ÚYÛˆÙBˆÛ‰İXİX[H]™Nˆ›İ[™È[ˆ\È\˜Ú]Xİ\™HYÜÈH]HY[[Û‚ˆÚ]\È\ÈH›ÛÚÚ[™È™\]Y\İˆœÈ\ÈØ\È[˜ÚY[[ÛX[[È‚ˆ
K™Ëˆ›^Hš\^H\ÈØİØ™\ˆ\İŠKˆH]\š\İXÈ\™HÛ›H™XÛÙÛš^™\Âˆ™\Ù[˜ÙHÙˆH]K[ZÙH˜\ÙKÛÈ\ÈÛÛ™\œØ][Û˜[]H\È™X]YˆHØ[YH\ÈH›ÛÚÚ[™ËZ[[Û™Kˆš^[™È\ÈÛİ[™YY™X[[[ˆÛ\ÜÚYšXØ][ÛˆÙˆH]\˜[˜ÙK›İHšYÙÙ\ˆ™YÙ^8 %İ]ÙˆØÛÜH›Ü‚ˆ\Èš^ˆ\È\İ^\İÈÛÈH]\™HÚ[™ÙHÈ]™Z]š[Üˆ\ÈBˆ[X™\˜]HXÚ\Ú[Û‹›İHÚ[[™YÜ™\ÜÚ[Û‹ˆˆˆ‚ˆÙ[ˆ\İÙXİHH×B‚ˆ\Ş[˜ÈYˆØ\\™J^[ØY
N‚ˆÙ[˜\[™
^[ØY
B‚ˆ[ÛšÙ^\]ÚœÙ]]Š›ÚXÙWÜÙ\ÜÚ[Û‹—ÜÙ[™‹Ø\\™JB‚ˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹HHØ^K^Hš\^H\ÈØİØ™\ˆ\İˆŠBˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹ŒÈKˆŠB‚ˆ]ØZ]Ü›Ø™J›ÚXÙWÜÙ\ÜÚ[Û‹Ù[œ™\ÜLH‹˜Ø[LH‹ŒŒ‹LLLUMNŒŒŠB‚ˆÈİ\œ™[Øİ[Y[Y™Z]š[ÜˆHÛÛ™\œØ][Û˜[Y[[Ûˆİ[Ü›İ[™ÂˆÈH›Ø™KˆYˆ\ÈÚ[™Ù\Ë\]H\È\İ[X™\˜][K‚ˆ\ÜÙ\Û\İÜİ]\ÊÙ[
HOH˜]˜Z[X›H‚‚‚™YˆÛ™^ÛØØ[
›ÚXÙWÜÙ\ÜÚ[Û‹ÙYZÙ^Nˆ[İ\ˆ[Z[]Nˆ[H
HOˆ]][YN‚ˆˆˆ”Ø[YH[HH›ÚXÙHÙ\ÜÚ[Ûˆ\Ù\Îˆ™^ÙYZÙ^H]\ÈÛØÚËÜˆ
ÍÈYˆ\İˆˆˆ‚ˆ›İÈH›ÚXÙWÜÙ\ÜÚ[Û‹—Û›İÊ
Bˆ^\ÈH
ÙYZÙ^HH›İË™]J
KÙYZÙ^J
JH	HÂˆİ\H]][YK˜ÛÛXš[™Jˆ›İË™]J
H
È[YY[J^\ÏY^\ÊKˆ[YJİ\‹Z[]JKˆš[™›Ï]›ÚXÙWÜÙ\ÜÚ[Û‹—İ‹ˆ
BˆYˆİ\H›İÎ‚ˆİ\
ÏH[YY[J^\ÏMÊBˆ™]\›ˆİ\‚‚˜\Ş[˜ÈYˆ\İİ\œÙ^WØ]ÍWÜ™]Üš]\×ØWİÜ›Û™×Û[Ù[İ[Y\İ[\
ˆ›ÚXÙWÜÙ\ÜÚ[Û‹]˜Z[Xš[]WÜİX‹[ÛšÙ^\]ÚŠN‚ˆÙ[ˆ\İÙXİHH×B‚ˆ\Ş[˜ÈYˆØ\\™J^[ØY
N‚ˆÙ[˜\[™
^[ØY
B‚ˆ[ÛšÙ^\]ÚœÙ]]Š›ÚXÙWÜÙ\ÜÚ[Û‹—ÜÙ[™‹Ø\\™JBˆ^XİYHÛ™^ÛØØ[
›ÚXÙWÜÙ\ÜÚ[Û‹ËMŠKœİ™[YJ‰VKI[KIY	R‰SN‰TÈŠB‚ˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹•\œÙ^KHŠBˆ]ØZ]Ü›Ø™J›ÚXÙWÜÙ\ÜÚ[Û‹Ù[œ™\ÜLH‹˜Ø[LH‹ŒŒŒLKLUNŒŒŠB‚ˆ\ÜÙ\Û\İÜİ]\ÊÙ[
HOH˜]˜Z[X›H‚ˆ\ÜÙ\]˜Z[Xš[]WÜİXˆOHÙ^XİYB‚‚˜\Ş[˜ÈYˆ\İÜØ]\™^Wİ[—ÍWİ\Ù\×İ]ÜØ]\™^Jˆ›ÚXÙWÜÙ\ÜÚ[Û‹]˜Z[Xš[]WÜİX‹[ÛšÙ^\]ÚŠN‚ˆÙ[ˆ\İÙXİHH×B‚ˆ\Ş[˜ÈYˆØ\\™J^[ØY
N‚ˆÙ[˜\[™
^[ØY
B‚ˆ[ÛšÙ^\]ÚœÙ]]Š›ÚXÙWÜÙ\ÜÚ[Û‹—ÜÙ[™‹Ø\\™JBˆ^XİYHÛ™^ÛØØ[
›ÚXÙWÜÙ\ÜÚ[Û‹KMŠKœİ™[YJ‰VKI[KIY	R‰SN‰TÈŠB‚ˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹”Ø]\™^HŠBˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹HŠBˆ]ØZ]Ü›Ø™J›ÚXÙWÜÙ\ÜÚ[Û‹Ù[œ™\ÜLH‹˜Ø[LH‹ŒNNNKLKLUMŒŒŠB‚ˆ\ÜÙ\Û\İÜİ]\ÊÙ[
HOH˜]˜Z[X›H‚ˆ\ÜÙ\]˜Z[Xš[]WÜİXˆOHÙ^XİYB‚‚˜\Ş[˜ÈYˆ\İÜØ]\™^WØY\››ÛÛ—ÜÙX\˜Ú\×İWİÚ[™İ×Û›İÛÛ™WÛZ[]Jˆ›ÚXÙWÜÙ\ÜÚ[Û‹]˜Z[Xš[]WÜİX‹[ÛšÙ^\]ÚŠN‚ˆÙ[ˆ\İÙXİHH×BˆÚ[™İÜÎˆ\İİ\VÙ]][YK]][YWWHH×B‚ˆ\Ş[˜ÈYˆØ\\™J^[ØY
N‚ˆÙ[˜\[™
^[ØY
B‚ˆ\Ş[˜ÈYˆ˜ZÙWÜÙX\˜Ú
Ù‹ÜÙ\ÜÚ[Û‹Ú[[İ\[™
N‚ˆÚ[™İÜË˜\[™

İ\[™
JBˆ™]\›ˆ›ÛÚÚ[™Ô™\İ[
ˆ›ÛÚÚ[™Óİ]ÛÛYKÓÓ‘“PÕˆY\ÜØYÙOH“Ü[š[™ÜÈ[ˆ]\ÙˆH^NˆØ]\™^H]NŒKˆ‹ˆ
B‚ˆ[ÛšÙ^\]ÚœÙ]]Š›ÚXÙWÜÙ\ÜÚ[Û‹—ÜÙ[™‹Ø\\™JBˆ[ÛšÙ^\]ÚœÙ]]Š˜\œÙ\šXÙ\ËZWÜ™X[[YKœÙX\˜ÚÙ^WÜ\‹˜ZÙWÜÙX\˜Ú
B‚ˆ›İÈH›ÚXÙWÜÙ\ÜÚ[Û‹—Û›İÊ
Bˆ^\ÈH
HH›İË™]J
KÙYZÙ^J
JH	HÂˆİ\H]][YK˜ÛÛXš[™Jˆ›İË™]J
H
È[YY[J^\ÏY^\ÊK[YJL‹
Kš[™›Ï]›ÚXÙWÜÙ\ÜÚ[Û‹—İ‚ˆ
Bˆ[™H]][YK˜ÛÛXš[™Jˆ›İË™]J
H
È[YY[J^\ÏY^\ÊK[YJMË
Kš[™›Ï]›ÚXÙWÜÙ\ÜÚ[Û‹—İ‚ˆ
BˆYˆ[™H›İÎ‚ˆİ\
ÏH[YY[J^\ÏMÊBˆ[™
ÏH[YY[J^\ÏMÊB‚ˆ]ØZ]ØØ[\—ÜØ^\Ê›ÚXÙWÜÙ\ÜÚ[Û‹”Ø]\™^HY\››ÛÛˆŠBˆ]ØZ]Ü›Ø™J›ÚXÙWÜÙ\ÜÚ[Û‹Ù[œ™\ÜLH‹˜Ø[LH‹ŒŒ‹LLLÕMŒŒŠB‚ˆ\ÜÙ\]˜Z[Xš[]WÜİXˆOH×Bˆ\ÜÙ\Ú[™İÜÈOHÊİ\[™
WBˆ\ÜÙ\Û\İÜİ]\ÊÙ[
HOH™^WÜ\ÛÜ[š[™ÜÈ‚