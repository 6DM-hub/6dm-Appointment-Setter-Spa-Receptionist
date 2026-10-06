"""Phase 1 customer resolution: lookup, links, guests, and tenant isolation."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from app.models import BookingProvider, Contact
from app.models.external_customer_link import ExternalCustomerLink
from app.services.appointment_booking_service import (
    BookingOutcome,
    _apply_provider_customer,
    _context,
    _failure,
)
from app.services.booking_adapters import BookingContext, GoogleCalendarAdapter
from app.services.booking_adapters.base import BookingProviderError, ExternalBooking
from app.services.booking_adapters.customers import CustomerLookupOutcome
from app.services.booking_adapters.providers.mangomint import MangomintAdapter
from app.services.booking_adapters.providers.mindbody import MindbodyAdapter
from app.services.booking_adapters.providers.vagaro import VagaroAdapter
from app.services.booking_adapters.providers.zenoti import ZenotiAdapter
from app.services.booking_adapters.spa_router import SpaBookingAdapter
from app.services.call_state import CallSession
from app.services.caller_identity import persist_caller_identity
from app.services.customer_links import (
    external_customer_link_statement,
    upsert_external_customer_link,
)
from app.services.phone_numbers import canonical_customer_phone, is_non_phone_caller_id
from tests.conftest import make_spa
from tests.test_square_customer_idempotency import _FakeTransport, _adapter, _ctx


def _arm_booking(transport: _FakeTransport) -> None:
    transport.catalog_items = [
        {
            "item_data": {
                "name": "Deep tissue massage",
                "variations": [
                    {"id": "var1", "version": 4, "item_variation_data": {"name": "Regular"}}
                ],
            }
        }
    ]
    transport.availabilities = [
        {
            "start_at": "2026-09-24T19:00:00Z",
            "location_id": "loc_123",
            "appointment_segments": [
                {
                    "team_member_id": "TM1",
                    "service_variation_id": "var1",
                    "service_variation_version": 4,
                    "duration_minutes": 60,
                }
            ],
        }
    ]


def _customer(customer_id: str, given: str, family: str | None = None) -> dict:
    row = {"id": customer_id, "given_name": given, "version": 3}
    if family:
        row["family_name"] = family
    return row


def _puts(transport: _FakeTransport) -> list:
    return [call for call in transport.calls if call[0] == "PUT"]


def _creates(transport: _FakeTransport) -> list:
    return [call for call in transport.calls if call[0] == "POST" and call[1] == "/v2/customers"]


class _Rows:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


@pytest.mark.asyncio
async def test_existing_square_customer_is_reused_without_update_or_duplicate(monkeypatch):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [[_customer("jane_square", "Jane", "Doe")]]
    adapter = _adapter(monkeypatch, transport)

    result = await adapter.create_booking(_ctx(customer_name="Jane Doe", caller_name="Jane Doe"))

    assert result.external_id == "sq_book_1"
    assert result.external_customer_id == "jane_square"
    assert result.customer_resolution == "reused"
    assert result.customer_display_name == "Jane Doe"
    booking = transport.calls_to("/v2/bookings")[0]
    assert booking["booking"]["customer_id"] == "jane_square"
    assert _creates(transport) == []
    assert _puts(transport) == []


@pytest.mark.asyncio
async def test_new_square_customer_is_created_once_after_zero_matches(monkeypatch):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [[]]
    transport.create_customer_results = [{"customer": {"id": "cust_new", "given_name": "Green"}}]
    adapter = _adapter(monkeypatch, transport)

    result = await adapter.create_booking(_ctx())

    assert result.external_customer_id == "cust_new"
    assert result.customer_resolution == "created"
    assert len(_creates(transport)) == 1
    created = _creates(transport)[0][2]
    assert created["phone_number"] == "+15550001"
    assert created["given_name"] == "Green"
    assert created["given_name"] != "Customer"
    assert transport.calls_to("/v2/bookings")[0]["booking"]["customer_id"] == "cust_new"
    assert _puts(transport) == []


@pytest.mark.asyncio
async def test_multiple_square_matches_are_not_auto_selected(monkeypatch):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [[
        _customer("cust_first", "Amy", "Adams"),
        _customer("cust_second", "Bea", "Brown"),
    ]]
    adapter = _adapter(monkeypatch, transport)

    with pytest.raises(BookingProviderError) as exc_info:
        await adapter.create_booking(_ctx(customer_name="Someone"))

    assert exc_info.value.code == "AMBIGUOUS_CUSTOMER"
    assert "cust_first" not in str(exc_info.value)
    assert "cust_second" not in str(exc_info.value)
    assert "Amy Adams" in str(exc_info.value)
    assert "Bea Brown" in str(exc_info.value)
    assert _creates(transport) == []
    assert _puts(transport) == []
    assert transport.calls_to("/v2/bookings") == []


@pytest.mark.asyncio
async def test_unique_caller_name_can_resolve_an_ambiguous_phone(monkeypatch):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [[
        _customer("cust_jane", "Jane", "Doe"),
        _customer("cust_john", "John", "Smith"),
    ]]
    adapter = _adapter(monkeypatch, transport)

    result = await adapter.create_booking(
        _ctx(caller_name="Jane Doe", customer_name="Jane Doe")
    )

    assert result.external_customer_id == "cust_jane"
    assert result.customer_resolution == "reused"
    assert _creates(transport) == []
    assert _puts(transport) == []


@pytest.mark.asyncio
async def test_shared_given_name_stays_ambiguous(monkeypatch):
    transport = _FakeTransport()
    transport.customer_search_results = [[
        _customer("cust_jane_1", "Jane", "Doe"),
        _customer("cust_jane_2", "Jane", "Smith"),
    ]]
    adapter = _adapter(monkeypatch, transport)

    with pytest.raises(BookingProviderError) as exc_info:
        await adapter.delegate._get_or_create_customer(_ctx(caller_name="Jane"))

    assert exc_info.value.code == "AMBIGUOUS_CUSTOMER"
    assert _creates(transport) == []
    assert _puts(transport) == []


@pytest.mark.asyncio
async def test_square_search_failure_does_not_create_a_customer(monkeypatch):
    transport = _FakeTransport()
    transport.customer_search_results = [
        BookingProviderError("Square customer search failed", code="HTTP_500")
    ]
    adapter = _adapter(monkeypatch, transport)

    with pytest.raises(BookingProviderError) as exc_info:
        await adapter.delegate._get_or_create_customer(_ctx())

    assert exc_info.value.code == "CUSTOMER_SEARCH_FAILED"
    assert _creates(transport) == []
    assert _puts(transport) == []


@pytest.mark.asyncio
async def test_local_contact_without_link_still_searches_square(monkeypatch):
    """A local contact is not the provider profile. Search still runs."""
    transport = _FakeTransport()
    transport.customer_search_results = [[_customer("jane_square", "Jane", "Doe")]]
    adapter = _adapter(monkeypatch, transport)
    ctx = _ctx(caller_name="Jane Doe")
    assert ctx.external_customer_id is None

    customer_id = await adapter.delegate._get_or_create_customer(ctx)

    assert customer_id == "jane_square"
    assert len(transport.calls_to("/v2/customers/search")) == 1
    assert _creates(transport) == []
    assert _puts(transport) == []


@pytest.mark.asyncio
async def test_square_customer_without_local_contact_is_reused_not_modified(monkeypatch):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [[
        _customer("jane_square", "Jane", "Doe") | {"email_address": "jane@spa.test"}
    ]]
    adapter = _adapter(monkeypatch, transport)

    result = await adapter.create_booking(_ctx())

    assert result.external_customer_id == "jane_square"
    assert result.customer_display_name == "Jane Doe"
    assert result.customer_email == "jane@spa.test"
    assert _creates(transport) == []
    assert _puts(transport) == []
    contact = Contact(phone_number="+15550001")
    _apply_provider_customer(contact, result)
    assert contact.first_name == "Jane"
    assert contact.last_name == "Doe"
    assert contact.email == "jane@spa.test"


def test_provider_name_does_not_overwrite_an_existing_local_name():
    contact = Contact(phone_number="+15550001", first_name="Janet", last_name="Kept")
    _apply_provider_customer(
        contact,
        ExternalBooking(
            provider="square",
            external_id="book",
            external_customer_id="jane_square",
            customer_display_name="Jane Doe",
        ),
    )
    assert contact.first_name == "Janet"
    assert contact.last_name == "Kept"


@pytest.mark.asyncio
async def test_equivalent_phone_formats_search_the_same_square_customer(monkeypatch):
    adapter = _adapter(monkeypatch, _FakeTransport())
    adapter.delegate.timezone_name = "America/Los_Angeles"
    formats = ["+14152345678", "(415) 234-5678", "4152345678", "415-234-5678"]

    for raw in formats:
        transport = _FakeTransport()
        transport.customer_search_results = [[_customer("same_customer", "Jane", "Doe")]]
        monkeypatch.setattr(adapter.delegate, "_request", transport)
        found = await adapter.find_customers_by_phone(raw)
        assert found.outcome == CustomerLookupOutcome.MATCHED
        assert found.customer is not None
        assert found.customer.external_customer_id == "same_customer"
        exact = transport.calls_to("/v2/customers/search")[0]["query"]["filter"]["phone_number"]["exact"]
        assert exact == "+14152345678"
        assert _creates(transport) == []


def test_phone_canonicalization_and_withheld_caller_ids():
    tz = "America/Los_Angeles"
    assert canonical_customer_phone("+14152345678", timezone_name=tz) == "+14152345678"
    assert canonical_customer_phone("(415) 234-5678", timezone_name=tz) == "+14152345678"
    assert canonical_customer_phone("4152345678", timezone_name=tz) == "+14152345678"
    assert canonical_customer_phone("1 (415) 234-5678", timezone_name=tz) == "+14152345678"
    assert canonical_customer_phone("+44 20 7946 0958", timezone_name=tz) == "+442079460958"
    # A non-NANP spa must not have a 10-digit number rewritten as +1.
    assert canonical_customer_phone("4152345678", timezone_name="Europe/London") is None
    for withheld in ("anonymous", "Restricted", "private", "withheld", "unknown", ""):
        assert is_non_phone_caller_id(withheld)
        assert canonical_customer_phone(withheld, timezone_name=tz) is None


@pytest.mark.asyncio
async def test_withheld_caller_id_does_not_search_or_create(monkeypatch):
    transport = _FakeTransport()
    adapter = _adapter(monkeypatch, transport)

    with pytest.raises(BookingProviderError) as exc_info:
        await adapter.delegate._get_or_create_customer(_ctx(customer_phone="anonymous"))

    assert exc_info.value.code == "INVALID_CALLER_ID"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_withheld_caller_id_is_not_stored_as_a_contact():
    db = AsyncMock()
    db.add = Mock()
    session = CallSession(
        "CA-private",
        "inbound",
        "anonymous",
        "+15550000001",
        tenant_id=str(uuid.uuid4()),
        timezone="America/Los_Angeles",
    )
    contact = await persist_caller_identity(db, session, "Jane Doe")
    assert contact is None
    db.add.assert_not_called()
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_john_booking_for_mary_does_not_rename_john(monkeypatch):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [[_customer("john_square", "John", "Smith")]]
    adapter = _adapter(monkeypatch, transport)

    result = await adapter.create_booking(
        _ctx(
            caller_name="John Smith",
            customer_name="Mary",
            guest_name="Mary",
            customer_phone="+15551111111",
        )
    )

    assert result.external_customer_id == "john_square"
    assert result.customer_display_name == "John Smith"
    booking = transport.calls_to("/v2/bookings")[0]["booking"]
    assert booking["customer_id"] == "john_square"
    assert booking["customer_note"] == "Guest: Mary"
    assert _creates(transport) == []
    assert _puts(transport) == []


@pytest.mark.asyncio
async def test_new_caller_booking_for_guest_creates_the_caller_not_the_guest(monkeypatch):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [[]]
    transport.create_customer_results = [{
        "customer": {"id": "john_new", "given_name": "John", "family_name": "Smith"}
    }]
    adapter = _adapter(monkeypatch, transport)

    result = await adapter.create_booking(
        _ctx(
            caller_name="John Smith",
            customer_name="Mary",
            guest_name="Mary",
            customer_phone="+15551111111",
        )
    )

    created = _creates(transport)[0][2]
    assert created["given_name"] == "John"
    assert created["family_name"] == "Smith"
    assert "Mary" not in created.values()
    assert result.external_customer_id == "john_new"
    note = transport.calls_to("/v2/bookings")[0]["booking"]["customer_note"]
    assert note == "Guest: Mary"


@pytest.mark.asyncio
async def test_guest_without_caller_name_does_not_create_a_customer(monkeypatch):
    transport = _FakeTransport()
    transport.customer_search_results = [[]]
    adapter = _adapter(monkeypatch, transport)

    with pytest.raises(BookingProviderError) as exc_info:
        await adapter.delegate._get_or_create_customer(
            _ctx(customer_name="Mary", guest_name="Mary", caller_name=None)
        )

    assert exc_info.value.code == "GUEST_UNRESOLVED"
    assert _creates(transport) == []
    assert _puts(transport) == []


def test_booking_context_keeps_guest_off_the_caller_profile():
    start = datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc)
    session = CallSession(
        "CA1",
        "inbound",
        "+14152345678",
        "+15550000001",
        timezone="America/Los_Angeles",
    )
    intent = SimpleNamespace(
        service_description="Massage",
        caller_name="John Smith",
        caller_email=None,
        guest_name="Mary",
        preferred_staff=None,
    )
    ctx = _context(
        GoogleCalendarAdapter(timezone_name="America/Los_Angeles"),
        session,
        intent,
        start,
        start + timedelta(hours=1),
    )
    assert ctx.customer_name == "John Smith"
    assert ctx.caller_name == "John Smith"
    assert ctx.guest_name == "Mary"
    assert "Guest: Mary" in (ctx.notes or "")
    assert ctx.customer_phone == "+14152345678"


@pytest.mark.asyncio
async def test_ambiguous_customer_asks_for_a_name_instead_of_a_provider_outage():
    session = CallSession("CA1", "inbound", "+15551234567", "+15550000001")
    result = await _failure(
        AsyncMock(),
        session,
        BookingProviderError(
            "More than one client profile is associated with this phone number. "
            "Names on file: Jane Doe; Mary Smith.",
            code="AMBIGUOUS_CUSTOMER",
        ),
    )
    assert result.outcome == BookingOutcome.MISSING_INFO
    assert "Jane Doe" in result.message
    assert session.booking_status == "collecting_details"


@pytest.mark.asyncio
async def test_external_link_is_created_for_the_active_tenant_only():
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    contact_id = uuid.uuid4()
    seen = []

    async def execute(statement):
        seen.append(statement)
        return _Rows(None)

    db = AsyncMock()
    db.add = Mock()
    db.execute = execute

    link = await upsert_external_customer_link(
        db,
        tenant_id=tenant_a,
        contact_id=contact_id,
        provider="square",
        external_customer_id="ABC123",
    )

    assert link is not None
    assert link.tenant_id == tenant_a
    assert link.external_customer_id == "ABC123"
    params = seen[0].compile().params
    assert tenant_a in params.values()
    assert tenant_b not in params.values()
    assert "ABC123" in params.values()

    other = external_customer_link_statement(tenant_b, "square", "ABC123")
    other_params = other.compile().params
    assert tenant_b in other_params.values()
    assert tenant_a not in other_params.values()


def test_same_external_customer_id_is_distinct_per_tenant():
    constraints = {
        constraint.name: [column.name for column in constraint.columns]
        for constraint in ExternalCustomerLink.__table__.constraints
        if constraint.name
    }
    assert constraints["uq_external_customer_tenant_provider_external"] == [
        "tenant_id",
        "provider",
        "external_customer_id",
    ]
    assert constraints["uq_external_customer_tenant_contact_provider"] == [
        "tenant_id",
        "contact_id",
        "provider",
    ]
    spa_a = ExternalCustomerLink(
        tenant_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        provider="square",
        external_customer_id="ABC123",
        last_verified_at=datetime.now(timezone.utc),
    )
    spa_b = ExternalCustomerLink(
        tenant_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        provider="square",
        external_customer_id="ABC123",
        last_verified_at=datetime.now(timezone.utc),
    )
    assert spa_a.tenant_id != spa_b.tenant_id
    assert spa_a.external_customer_id == spa_b.external_customer_id


def test_each_spa_uses_its_own_square_credentials():
    spa_a = make_spa(
        name="Spa A",
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "token-spa-a", "location_id": "loc-a"},
        timezone="America/Los_Angeles",
    )
    spa_b = make_spa(
        name="Spa B",
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "token-spa-b", "location_id": "loc-b"},
        timezone="America/Chicago",
    )
    adapter_a = SpaBookingAdapter(spa_a)
    adapter_b = SpaBookingAdapter(spa_b)
    assert adapter_a.delegate.access_token == "token-spa-a"
    assert adapter_a.delegate.location_id == "loc-a"
    assert adapter_a.delegate.timezone_name == "America/Los_Angeles"
    assert adapter_b.delegate.access_token == "token-spa-b"
    assert adapter_b.delegate.location_id == "loc-b"
    assert adapter_a.supports_customer_lookup is True
    assert adapter_b.supports_customer_creation is True


@pytest.mark.asyncio
async def test_google_calendar_has_no_customer_directory():
    adapter = GoogleCalendarAdapter(timezone_name="America/Los_Angeles")
    assert adapter.supports_customer_lookup is False
    assert adapter.supports_customer_creation is False
    lookup = await adapter.find_customers_by_phone("+14152345678")
    assert lookup.outcome == CustomerLookupOutcome.UNSUPPORTED
    start = datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc)
    booking = await adapter.create_booking(
        BookingContext(
            start=start,
            end=start + timedelta(hours=1),
            title="Massage",
            customer_phone="+14152345678",
            customer_name="Jane Doe",
        )
    )
    assert booking.external_customer_id is None
    assert booking.provider == "google_calendar"

    link = await upsert_external_customer_link(
        AsyncMock(),
        tenant_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        provider="google_calendar",
        external_customer_id="not-a-customer",
    )
    assert link is None


@pytest.mark.asyncio
async def test_inactive_square_replay_is_not_treated_as_a_new_booking(monkeypatch):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [[_customer("rick_square", "Rick", "Sanchez")]]
    transport.created_booking = {
        "id": "jb2z0gdgg0x0le",
        "status": "CANCELLED_BY_SELLER",
    }
    adapter = _adapter(monkeypatch, transport)

    with pytest.raises(BookingProviderError) as exc_info:
        await adapter.create_booking(
            _ctx(
                caller_name="Rayn",
                customer_name="Rayn",
                customer_phone="+15551111111",
            )
        )

    assert exc_info.value.code == "INACTIVE_BOOKING"
    assert len(transport.calls_to("/v2/bookings")) == 1


@pytest.mark.asyncio
async def test_accepted_square_booking_still_returns_the_external_id(monkeypatch):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [[_customer("rick_square", "Rick", "Sanchez")]]
    transport.created_booking = {"id": "sq_active", "status": "ACCEPTED"}
    adapter = _adapter(monkeypatch, transport)

    result = await adapter.create_booking(
        _ctx(
            caller_name="Rick Sanchez",
            customer_name="Rick Sanchez",
            customer_phone="+15551111111",
        )
    )

    assert result.external_id == "sq_active"


def _note(transport: _FakeTransport) -> str | None:
    bookings = transport.calls_to("/v2/bookings")
    assert bookings
    return bookings[0]["booking"].get("customer_note")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "adapter_cls", "config"),
    [
        (
            BookingProvider.MINDBODY,
            MindbodyAdapter,
            {"site_id": "1", "api_key": "k", "source_name": "n", "source_password": "p"},
        ),
        (
            BookingProvider.MANGOMINT,
            MangomintAdapter,
            {"api_key": "k", "location_id": "loc"},
        ),
        (
            BookingProvider.VAGARO,
            VagaroAdapter,
            {"api_key": "k", "business_id": "biz"},
        ),
        (
            BookingProvider.ZENOTI,
            ZenotiAdapter,
            {"api_key": "k", "center_id": "center"},
        ),
    ],
)
async def test_unimplemented_providers_stay_unsupported(provider, adapter_cls, config):
    assert adapter_cls.implemented is False
    assert adapter_cls.supports_customer_lookup is False
    assert adapter_cls.supports_customer_creation is False
    spa = make_spa(booking_provider=provider, booking_config=config)
    adapter = SpaBookingAdapter(spa)
    assert adapter.supports_customer_lookup is False
    assert adapter.supports_customer_creation is False
    assert type(adapter.delegate).__name__ == "ProviderNotConfiguredAdapter"
    lookup = await adapter.find_customers_by_phone("+14152345678")
    assert lookup.outcome == CustomerLookupOutcome.UNSUPPORTED
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


@pytest.mark.asyncio
async def test_same_square_name_does_not_add_a_caller_note(monkeypatch):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [[_customer("rick_square", "Rick", "Sanchez")]]
    adapter = _adapter(monkeypatch, transport)

    result = await adapter.create_booking(
        _ctx(
            caller_name="Rick Sanchez",
            customer_name="Rick Sanchez",
            customer_phone="+15551111111",
        )
    )

    assert result.external_customer_id == "rick_square"
    assert result.customer_resolution == "reused"
    assert _note(transport) is None
    assert _creates(transport) == []
    assert _puts(transport) == []


@pytest.mark.asyncio
async def test_case_and_spacing_do_not_count_as_a_different_caller(monkeypatch):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [
        [_customer("rick_square", "Rick", "Sanchez")],
        [_customer("rick_square", "Rick", "Sanchez")],
    ]
    adapter = _adapter(monkeypatch, transport)

    await adapter.create_booking(
        _ctx(
            caller_name="  rick   sanchez ",
            customer_name="  rick   sanchez ",
            customer_phone="+15551111111",
        )
    )
    assert _note(transport) is None

    await adapter.create_booking(
        _ctx(
            caller_name="Rick Sanchez.",
            customer_name="Rick Sanchez.",
            customer_phone="+15551111111",
        )
    )
    notes = [payload["booking"].get("customer_note") for payload in transport.calls_to("/v2/bookings")]
    assert notes == [None, None]
    assert _creates(transport) == []
    assert _puts(transport) == []


@pytest.mark.asyncio
async def test_different_caller_name_is_appended_without_renaming_the_customer(
    monkeypatch, caplog
):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [[_customer("rick_square", "Rick", "Sanchez")]]
    adapter = _adapter(monkeypatch, transport)

    with caplog.at_level("INFO", logger="receptionist.truth"):
        result = await adapter.create_booking(
            _ctx(
                caller_name="Rayn",
                customer_name="Rayn",
                customer_phone="+12025550123",
                notes="Booked by the AI agent during call CA-test.",
            )
        )

    assert result.external_customer_id == "rick_square"
    assert result.customer_display_name == "Rick Sanchez"
    assert result.customer_resolution == "reused"
    assert _note(transport) == "Caller name provided during call: Rayn"
    assert _creates(transport) == []
    assert _puts(transport) == []
    mismatch_lines = [
        record.message
        for record in caplog.records
        if "BOOKING_CALLER_NAME_MISMATCH_NOTE_ADDED" in record.message
    ]
    assert len(mismatch_lines) == 1
    assert "Rayn" not in mismatch_lines[0]
    assert "Rick" not in mismatch_lines[0]
    assert "+12025550123" not in mismatch_lines[0]


@pytest.mark.asyncio
async def test_existing_booking_note_is_kept_when_the_caller_name_differs(monkeypatch):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [[_customer("rick_square", "Rick", "Sanchez")]]
    adapter = _adapter(monkeypatch, transport)

    await adapter.create_booking(
        _ctx(
            caller_name="Rayn",
            customer_name="Rayn",
            customer_phone="+15551111111",
            notes="Prefers female therapist.",
        )
    )

    assert _note(transport) == (
        "Prefers female therapist.\nCaller name provided during call: Rayn"
    )
    assert _creates(transport) == []
    assert _puts(transport) == []


@pytest.mark.asyncio
async def test_missing_caller_name_does_not_add_a_caller_note(monkeypatch):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [[_customer("rick_square", "Rick", "Sanchez")]]
    adapter = _adapter(monkeypatch, transport)

    result = await adapter.create_booking(
        _ctx(
            caller_name=None,
            customer_name=None,
            customer_phone="+15551111111",
        )
    )

    assert result.external_customer_id == "rick_square"
    assert _note(transport) is None
    assert _creates(transport) == []


@pytest.mark.asyncio
async def test_new_customer_with_the_supplied_name_has_no_mismatch_note(monkeypatch):
    transport = _FakeTransport()
    _arm_booking(transport)
    transport.customer_search_results = [[]]
    transport.create_customer_results = [{
        "customer": {"id": "rayn_new", "given_name": "Rayn", "phone_number": "+15551111111"}
    }]
    adapter = _adapter(monkeypatch, transport)

    result = await adapter.create_booking(
        _ctx(
            caller_name="Rayn",
            customer_name="Rayn",
            customer_phone="+15551111111",
        )
    )

    assert result.external_customer_id == "rayn_new"
    assert result.customer_resolution == "created"
    assert result.customer_display_name == "Rayn"
    assert _note(transport) is None
    assert len(_creates(transport)) == 1
    assert _puts(transport) == []
