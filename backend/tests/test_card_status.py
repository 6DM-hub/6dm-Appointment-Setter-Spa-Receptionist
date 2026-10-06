"""Phase 2 saved-card status. Lookup only: no SMS, no CreateCard, no charge."""
from datetime import datetime, timedelta, timezone
import uuid
from types import SimpleNamespace

import pytest

from app.models import BookingProvider
from app.models.appointment import CardStatus
from app.services import appointment_booking_service
from app.services.appointment_booking_service import (
    BookingOutcome,
    confirm_booking,
    stage_booking,
)
from app.services.booking_adapters import BookingContext, GoogleCalendarAdapter, SpaBookingAdapter
from app.services.booking_adapters.base import BookingProviderError
from app.services.booking_adapters.providers.mindbody import MindbodyAdapter
from app.services.booking_adapters.providers.square import SquareAdapter
from app.services.booking_adapters.saved_payments import (
    SavedPaymentLookup,
    SavedPaymentLookupOutcome,
)
from app.services.booking_state import arm_verified_proposal
from app.services.call_state import CallSession
from app.services.card_status import card_status_for_booked_appointment, is_guest_booking
from app.services.grok_service import AppointmentIntent
from tests.conftest import make_spa


def _usable(**overrides) -> dict:
    card = {
        "id": "ccof:usable",
        "enabled": True,
        "exp_month": 12,
        "exp_year": 2030,
    }
    card.update(overrides)
    return card


def _spa(*, card_required: bool, provider: BookingProvider = BookingProvider.SQUARE, **extra):
    config = extra.pop(
        "booking_config",
        {"access_token": "token-spa-a", "location_id": "loc-a"},
    )
    spa = make_spa(
        booking_provider=provider,
        booking_config=config,
        timezone="America/Los_Angeles",
        **extra,
    )
    spa.payment_policy = {"card_required": card_required, "collection_mode": "none"}
    return spa


class _Cards:
    def __init__(self, cards=None, error: Exception | None = None) -> None:
        self.cards = cards
        self.error = error
        self.calls: list[tuple] = []

    async def __call__(self, method, path, *, json=None, params=None):
        self.calls.append((method, path, params or {}, json or {}))
        if method == "POST" and path in {"/v2/cards", "/v2/payments"}:
            raise AssertionError(f"Phase 2 must not call {method} {path}")
        if method == "GET" and path == "/v2/cards":
            if self.error:
                raise self.error
            return {"cards": self.cards if self.cards is not None else []}
        raise AssertionError(f"unexpected {method} {path}")


def _square(spa, transport: _Cards) -> SpaBookingAdapter:
    adapter = SpaBookingAdapter(spa)
    adapter.delegate._request = transport
    return adapter


async def _status(spa, adapter, **kwargs) -> CardStatus:
    defaults = dict(
        spa=spa,
        adapter=adapter,
        external_customer_id="jane_square",
        caller_name="Jane Doe",
        guest_name=None,
    )
    defaults.update(kwargs)
    return await card_status_for_booked_appointment(**defaults)


@pytest.mark.asyncio
async def test_card_not_required_does_not_list_cards():
    spa = _spa(card_required=False)
    transport = _Cards(cards=[_usable()])
    adapter = _square(spa, transport)

    status = await _status(spa, adapter)

    assert status is CardStatus.NOT_REQUIRED
    assert transport.calls == []


@pytest.mark.asyncio
async def test_usable_square_card_is_confirmed_without_creating_or_charging():
    spa = _spa(card_required=True)
    transport = _Cards(cards=[_usable()])
    adapter = _square(spa, transport)

    status = await _status(spa, adapter, external_customer_id="jane_square")

    assert status is CardStatus.CARD_CONFIRMED
    assert transport.calls == [
        (
            "GET",
            "/v2/cards",
            {"customer_id": "jane_square", "include_disabled": "true"},
            {},
        )
    ]


@pytest.mark.asyncio
async def test_successful_empty_card_list_is_pending_not_assumed():
    spa = _spa(card_required=True)
    transport = _Cards(cards=[])
    adapter = _square(spa, transport)

    status = await _status(spa, adapter, external_customer_id="new_square")

    assert status is CardStatus.PENDING_CARD
    assert transport.calls[0][0] == "GET"


@pytest.mark.asyncio
async def test_expired_card_only_is_pending():
    spa = _spa(card_required=True)
    transport = _Cards(cards=[_usable(id="ccof:old", exp_month=1, exp_year=2020)])
    adapter = _square(spa, transport)

    assert await _status(spa, adapter) is CardStatus.PENDING_CARD


@pytest.mark.asyncio
async def test_disabled_card_only_is_pending():
    spa = _spa(card_required=True)
    transport = _Cards(cards=[_usable(id="ccof:off", enabled=False)])
    adapter = _square(spa, transport)

    assert await _status(spa, adapter) is CardStatus.PENDING_CARD


@pytest.mark.asyncio
async def test_one_usable_card_among_invalid_cards_is_confirmed():
    spa = _spa(card_required=True)
    transport = _Cards(
        cards=[
            _usable(id="ccof:old", exp_year=2020),
            _usable(id="ccof:off", enabled=False),
            _usable(id="ccof:good"),
        ]
    )
    adapter = _square(spa, transport)

    status = await _status(spa, adapter)
    assert status is CardStatus.CARD_CONFIRMED
    assert adapter.delegate._request.calls[0][2]["customer_id"] == "jane_square"


@pytest.mark.asyncio
async def test_square_card_lookup_failure_is_unknown_not_pending():
    spa = _spa(card_required=True)
    transport = _Cards(error=BookingProviderError("Square unavailable", code="HTTP_500"))
    adapter = _square(spa, transport)

    status = await _status(spa, adapter)

    assert status is CardStatus.UNKNOWN
    assert status is not CardStatus.PENDING_CARD
    assert transport.calls[0][0] == "GET"


@pytest.mark.asyncio
async def test_malformed_card_payload_is_unknown():
    spa = _spa(card_required=True)

    async def _request(method, path, *, json=None, params=None):
        return {"cards": "not-a-list"}

    adapter = _square(spa, _Cards())
    adapter.delegate._request = _request

    assert await _status(spa, adapter) is CardStatus.UNKNOWN


@pytest.mark.asyncio
async def test_google_card_required_is_not_supported_and_does_not_invent_cards():
    spa = _spa(
        card_required=True,
        provider=BookingProvider.GOOGLE_CALENDAR,
        booking_config={"google_calendar_id": "cal"},
    )
    adapter = GoogleCalendarAdapter(timezone_name="America/Los_Angeles")
    assert adapter.supports_saved_payment_method_lookup is False

    status = await _status(
        spa,
        adapter,
        external_customer_id="not-a-customer",
    )

    assert status is CardStatus.NOT_SUPPORTED
    lookup = await adapter.list_saved_payment_methods("not-a-customer")
    assert lookup.outcome is SavedPaymentLookupOutcome.UNSUPPORTED


@pytest.mark.asyncio
async def test_stub_provider_has_no_saved_card_support():
    assert MindbodyAdapter.supports_saved_payment_method_lookup is False
    spa = make_spa(
        booking_provider=BookingProvider.MINDBODY,
        booking_config={
            "site_id": "1",
            "api_key": "k",
            "source_name": "n",
            "source_password": "p",
        },
    )
    spa.payment_policy = {"card_required": True, "collection_mode": "none"}
    adapter = SpaBookingAdapter(spa)
    assert adapter.supports_saved_payment_method_lookup is False
    assert type(adapter.delegate).__name__ == "ProviderNotConfiguredAdapter"
    lookup = await adapter.list_saved_payment_methods("someone")
    assert lookup.outcome is SavedPaymentLookupOutcome.UNSUPPORTED
    status = await _status(spa, adapter, external_customer_id="someone")
    assert status is CardStatus.NOT_SUPPORTED
    start = datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc)
    with pytest.raises(BookingProviderError):
        await adapter.create_booking(
            BookingContext(
                start=start,
                end=start + timedelta(hours=1),
                title="Massage",
                customer_phone="+14152345678",
            )
        )


def test_guest_booking_is_not_a_self_booking():
    assert is_guest_booking("John Smith", "Mary") is True
    assert is_guest_booking("John Smith", "John Smith") is False
    assert is_guest_booking("John Smith", None) is False


@pytest.mark.asyncio
async def test_john_books_for_mary_does_not_use_johns_card():
    spa = _spa(card_required=True)
    transport = _Cards(cards=[_usable(id="ccof:john")])
    adapter = _square(spa, transport)

    status = await _status(
        spa,
        adapter,
        external_customer_id="john_square",
        caller_name="John Smith",
        guest_name="Mary",
    )

    assert status is CardStatus.UNKNOWN
    assert status is not CardStatus.CARD_CONFIRMED
    assert status is not CardStatus.PENDING_CARD
    assert transport.calls == []


@pytest.mark.asyncio
async def test_john_booking_for_himself_uses_his_card():
    spa = _spa(card_required=True)
    transport = _Cards(cards=[_usable(id="ccof:john")])
    adapter = _square(spa, transport)

    status = await _status(
        spa,
        adapter,
        external_customer_id="john_square",
        caller_name="John Smith",
        guest_name="John Smith",
    )

    assert status is CardStatus.CARD_CONFIRMED
    assert transport.calls[0][2]["customer_id"] == "john_square"


def test_unevaluated_appointment_defaults_to_unknown():
    from app.models.appointment import Appointment
    from app.schemas.appointment import AppointmentRead

    column = Appointment.__table__.c.card_status
    assert column.default.arg is CardStatus.UNKNOWN
    assert str(column.server_default.arg) == "unknown"
    assert AppointmentRead.model_fields["card_status"].default is CardStatus.UNKNOWN


@pytest.mark.asyncio
async def test_required_card_without_a_lookup_result_is_not_left_not_required():
    spa = _spa(card_required=True)

    class _NoResult:
        supports_saved_payment_method_lookup = True

        async def has_usable_saved_payment_method(self, external_customer_id: str):
            return SimpleNamespace(outcome=None)

    status = await card_status_for_booked_appointment(
        spa=spa,
        adapter=_NoResult(),
        external_customer_id="jane_square",
        caller_name="Jane Doe",
        guest_name=None,
    )
    assert status is CardStatus.UNKNOWN
    assert status is not CardStatus.NOT_REQUIRED


class _PagedCards:
    def __init__(self, pages: dict, fail_cursor: str | None = None) -> None:
        self.pages = pages
        self.fail_cursor = fail_cursor
        self.calls: list[dict] = []

    async def __call__(self, method, path, *, json=None, params=None):
        params = params or {}
        self.calls.append(dict(params))
        if method != "GET" or path != "/v2/cards":
            raise AssertionError(f"unexpected {method} {path}")
        cursor = params.get("cursor")
        if self.fail_cursor is not None and cursor == self.fail_cursor:
            raise BookingProviderError("Square card page failed", code="HTTP_500")
        page = self.pages[cursor]
        return page


@pytest.mark.asyncio
async def test_usable_card_on_second_page_is_confirmed():
    spa = _spa(card_required=True)
    transport = _PagedCards(
        {
            None: {
                "cards": [
                    _usable(id="ccof:old", exp_year=2020),
                    _usable(id="ccof:off", enabled=False),
                ],
                "cursor": "page-2",
            },
            "page-2": {"cards": [_usable(id="ccof:good")]},
        }
    )
    adapter = _square(spa, transport)

    status = await _status(spa, adapter, external_customer_id="jane_square")

    assert status is CardStatus.CARD_CONFIRMED
    assert [call.get("cursor") for call in transport.calls] == [None, "page-2"]
    assert {call["customer_id"] for call in transport.calls} == {"jane_square"}


@pytest.mark.asyncio
async def test_second_page_error_is_unknown_not_pending():
    spa = _spa(card_required=True)
    transport = _PagedCards(
        {
            None: {
                "cards": [_usable(id="ccof:old", exp_year=2020)],
                "cursor": "page-2",
            },
        },
        fail_cursor="page-2",
    )
    adapter = _square(spa, transport)

    status = await _status(spa, adapter, external_customer_id="jane_square")

    assert status is CardStatus.UNKNOWN
    assert status is not CardStatus.PENDING_CARD
    assert [call.get("cursor") for call in transport.calls] == [None, "page-2"]


@pytest.mark.asyncio
async def test_repeated_card_cursor_is_unknown():
    spa = _spa(card_required=True)
    transport = _PagedCards(
        {
            None: {
                "cards": [_usable(id="ccof:old", exp_year=2020)],
                "cursor": "again",
            },
            "again": {
                "cards": [_usable(id="ccof:old-2", exp_year=2020)],
                "cursor": "again",
            },
        }
    )
    adapter = _square(spa, transport)

    status = await _status(spa, adapter, external_customer_id="jane_square")

    assert status is CardStatus.UNKNOWN
    assert status is not CardStatus.PENDING_CARD
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_card_page_bound_does_not_claim_no_card():
    """Fifty pages is the defensive stop. A cursor still outstanding is unknown."""
    spa = _spa(card_required=True)
    pages: dict = {}
    cursor = None
    for index in range(50):
        next_cursor = f"page-{index + 1}"
        pages[cursor] = {
            "cards": [_usable(id=f"ccof:old-{index}", exp_year=2020)],
            "cursor": next_cursor,
        }
        cursor = next_cursor
    transport = _PagedCards(pages)
    adapter = _square(spa, transport)

    status = await _status(spa, adapter, external_customer_id="jane_square")

    assert status is CardStatus.UNKNOWN
    assert status is not CardStatus.PENDING_CARD
    assert len(transport.calls) == 50


@pytest.mark.asyncio
async def test_resolved_guest_customer_is_checked_instead_of_the_caller():
    """The decision uses a guest provider id only when one is actually supplied.

    The live booking path does not have that id yet. This test covers the
    branch that must use it, and must not consult the caller, when it exists.
    """
    spa = _spa(card_required=True)
    transport = _Cards(cards=[])
    adapter = _square(spa, transport)

    status = await _status(
        spa,
        adapter,
        external_customer_id="CUS_JOHN",
        caller_name="John Smith",
        guest_name="Mary",
        guest_external_customer_id="CUS_MARY",
    )

    assert status is CardStatus.PENDING_CARD
    assert transport.calls[0][2]["customer_id"] == "CUS_MARY"
    assert all(call[2].get("customer_id") != "CUS_JOHN" for call in transport.calls)


@pytest.mark.asyncio
async def test_card_lookup_uses_only_the_active_spas_customer_and_token():
    spa_a = _spa(card_required=True, name="Spa A")
    spa_b = _spa(
        card_required=True,
        name="Spa B",
        booking_config={"access_token": "token-spa-b", "location_id": "loc-b"},
    )
    seen: list[tuple] = []

    async def _request(method, path, *, json=None, params=None):
        seen.append((method, path, params, spa_a_adapter.delegate.access_token))
        return {"cards": [_usable()]}

    spa_a_adapter = _square(spa_a, _Cards())
    spa_b_adapter = SpaBookingAdapter(spa_b)
    spa_a_adapter.delegate._request = _request

    status = await _status(
        spa_a,
        spa_a_adapter,
        external_customer_id="customer-spa-a",
    )

    assert status is CardStatus.CARD_CONFIRMED
    assert seen == [
        (
            "GET",
            "/v2/cards",
            {"customer_id": "customer-spa-a", "include_disabled": "true"},
            "token-spa-a",
        )
    ]
    assert spa_b_adapter.delegate.access_token == "token-spa-b"
    assert "customer-spa-b" not in str(seen)
    assert "token-spa-b" not in str(seen)


class _RecordingAdapter:
    provider = "square"
    supports_saved_payment_method_lookup = True

    def __init__(self) -> None:
        self.lookups: list[str] = []
        self.default_duration_minutes = 60
        self.default_title = "Spa Service Appointment"

    async def has_usable_saved_payment_method(self, external_customer_id: str):
        self.lookups.append(external_customer_id)
        return SavedPaymentLookup(outcome=SavedPaymentLookupOutcome.USABLE, usable_count=1)


@pytest.mark.asyncio
async def test_failed_booking_does_not_run_saved_card_lookup(monkeypatch):
    spa = _spa(
        card_required=True,
        business_hours={"thu": [{"open": "10:00", "close": "18:00"}]},
        services=[{"name": "Deep tissue massage", "duration_minutes": 60}],
    )
    recording = _RecordingAdapter()

    async def _boom(_ctx):
        raise BookingProviderError("The requested appointment is no longer available")

    monkeypatch.setattr(appointment_booking_service, "_load_spa", lambda *_: _async(spa))
    monkeypatch.setattr(appointment_booking_service, "get_booking_adapter", lambda **_: recording)
    monkeypatch.setattr(
        appointment_booking_service,
        "_resolve_contact",
        lambda *_: _async(SimpleNamespace(id=uuid.uuid4())),
    )
    monkeypatch.setattr(appointment_booking_service, "_has_conflict", lambda *_a, **_k: _async(False))
    monkeypatch.setattr(appointment_booking_service, "_load_by_intent_key", lambda *_: _async(None))
    monkeypatch.setattr(appointment_booking_service, "_load_call_log_id", lambda *_: _async(None))
    monkeypatch.setattr(
        appointment_booking_service,
        "persist_caller_identity",
        lambda *_a, **_k: _async(None),
    )
    recording.create_booking = _boom
    recording.check_availability = lambda _ctx: _async(
        SimpleNamespace(available=True, reason=None, slot=None, alternatives=())
    )
    recording.calendar_label = "Square"
    recording.default_title = "Spa Service Appointment"
    recording.delegate = SimpleNamespace(timezone_name="America/Los_Angeles")

    class _DB:
        def add(self, _obj):
            return None

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def rollback(self):
            return None

        async def refresh(self, _obj):
            return None

    session = CallSession(
        "call-card",
        "inbound",
        "+14152345678",
        "+15550000001",
        tenant_id=str(spa.id),
    )
    intent = AppointmentIntent(
        intent="schedule",
        confidence=1.0,
        requested_start_iso="2026-09-24T14:00:00",
        service_description="Deep tissue massage",
        caller_name="Jane Doe",
    )
    staged = await stage_booking(_DB(), session, intent)
    assert staged.outcome is BookingOutcome.DRAFT
    arm_verified_proposal(session)
    result = await confirm_booking(_DB(), session)

    assert result.outcome is not BookingOutcome.BOOKED
    assert recording.lookups == []


@pytest.mark.asyncio
async def test_booked_appointment_keeps_unknown_when_card_lookup_fails(monkeypatch):
    spa = _spa(card_required=True)
    calls = []

    class _Adapter:
        provider = "square"
        calendar_label = "Square"
        supports_saved_payment_method_lookup = True
        external_customer_id = "jane_square"

        async def has_usable_saved_payment_method(self, external_customer_id: str):
            calls.append(external_customer_id)
            raise BookingProviderError("timeout")

    status = await card_status_for_booked_appointment(
        spa=spa,
        adapter=_Adapter(),
        external_customer_id="jane_square",
        caller_name="Jane Doe",
        guest_name=None,
    )
    assert calls == ["jane_square"]
    assert status is CardStatus.UNKNOWN


def test_phase_2_square_class_does_not_create_cards_or_payments():
    import inspect

    source = inspect.getsource(SquareAdapter)
    assert '"/v2/cards"' in source
    assert "/v2/payments" not in source
    assert "CreatePayment" not in source
    assert "save_card_on_file" in source


async def _async(value):
    return value
