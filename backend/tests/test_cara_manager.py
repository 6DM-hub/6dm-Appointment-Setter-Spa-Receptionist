import uuid
from datetime import timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from app.models.base import Base
from app.models import Appointment, AppointmentStatus, Contact, SpaAccount, User, UserRole
from app.models.cara_manager import CaraDelivery
from app.services import cara_manager as m


def future_open_time(days=2):
    """Keep real-clock tests inside the fixture's configured local hours."""
    return (m.now_utc().astimezone(ZoneInfo("America/Chicago")) + timedelta(days=days)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )


@compiles(JSONB, "sqlite")
def sqlite_json(type_, compiler, **kw):
    return "JSON"


@pytest_asyncio.fixture
async def data():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    @event.listens_for(engine.sync_engine, "connect")
    def functions(connection, record):
        connection.create_function("num_nonnulls", -1, lambda *args: sum(x is not None for x in args))
        connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        spa = SpaAccount(id=uuid.uuid4(), name="Test Spa", timezone="America/Chicago",
                         business_hours={day: [{"open": "00:00", "close": "23:59"}] for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
                         services=[{"name": "Swedish Massage", "duration_minutes": 60, "price": "$100"}])
        owner = User(id=uuid.uuid4(), email="owner@example.test", hashed_password="test", role=UserRole.SPA_ADMIN, tenant_id=spa.id)
        master = User(id=uuid.uuid4(), email="master@example.test", hashed_password="test", role=UserRole.SPA_ADMIN,
                      tenant_id=spa.id, is_business_master=True)
        db.add_all([spa, owner, master]); await db.flush()
        customer = Contact(id=uuid.uuid4(), tenant_id=spa.id, first_name="Test Alice", phone_number="+19995550100",
                           extra_metadata={"is_test_customer": True, "marketing_sms_opt_in": True})
        db.add(customer); await db.flush()
        end = m.now_utc() - timedelta(days=120)
        db.add(Appointment(tenant_id=spa.id, contact_id=customer.id, title="Swedish Massage", start_time=end-timedelta(minutes=60), end_time=end, status=AppointmentStatus.COMPLETED))
        await db.commit()
        yield db, spa, owner, master, customer
    await engine.dispose()


async def campaign(data):
    db, spa, owner, master, customer = data
    c = await m.prepare(db, spa.id, owner.id, "90-day reactivation campaign", True, None)
    await m.approve(db, spa.id, master.id, c.id, c.proposal_hash)
    return c


@pytest.mark.asyncio
async def test_complete_persisted_flow_and_idempotency(data):
    db, spa, owner, master, customer = data
    c = await campaign(data)
    assert c.proposal["audience"][0]["offer"]["duration_minutes"] == 60
    await m.execute_test(db, spa.id, owner.id, c.id)
    await m.execute_test(db, spa.id, owner.id, c.id)
    deliveries = list((await db.execute(select(CaraDelivery))).scalars())
    assert len(deliveries) == 1
    d = deliveries[0]
    await m.reply_test(db, spa.id, owner.id, d.id, "YES")
    start = m.now_utc() + timedelta(days=2)
    await m.book_test(db, spa.id, owner.id, d.id, start)
    first = dict(d.booking)
    await m.book_test(db, spa.id, owner.id, d.id, start)
    assert d.booking == first
    r = await m.results(db, spa.id, c)
    assert (r["messages_sent_test"], r["responses"], r["appointments_booked_test"]) == (1, 1, 1)
    assert r["verified_revenue"] is None
    assert any(e["action"] == "test_booking_confirmed" for e in r["audit"])


@pytest.mark.asyncio
async def test_approval_required_and_price_changes_invalidate(data):
    db, spa, owner, master, customer = data
    c = await m.prepare(db, spa.id, owner.id, "90-day reactivation", True, None)
    with pytest.raises(HTTPException):
        await m.execute_test(db, spa.id, owner.id, c.id)
    await m.approve(db, spa.id, master.id, c.id, c.proposal_hash)
    spa.services = [{"name": "Swedish Massage", "duration_minutes": 60, "price": "$110"}]
    await db.commit()
    with pytest.raises(HTTPException, match="") as e:
        await m.execute_test(db, spa.id, owner.id, c.id)
    assert e.value.status_code == 409
    assert not list((await db.execute(select(CaraDelivery))).scalars())


@pytest.mark.asyncio
async def test_optout_rechecked_and_tenant_isolation(data):
    db, spa, owner, master, customer = data
    c = await campaign(data)
    customer.extra_metadata = dict(customer.extra_metadata, sms_opt_out=True)
    await db.commit()
    await m.execute_test(db, spa.id, owner.id, c.id)
    assert (await m.results(db, spa.id, c))["messages_sent_test"] == 0
    with pytest.raises(HTTPException) as e:
        await m.load_campaign(db, uuid.uuid4(), c.id)
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_failed_recheck_never_confirms(data):
    db, spa, owner, master, customer = data
    c = await campaign(data)
    await m.execute_test(db, spa.id, owner.id, c.id)
    d = (await db.execute(select(CaraDelivery))).scalar_one()
    await m.reply_test(db, spa.id, owner.id, d.id, "YES")
    gateway = AsyncMock()
    gateway.available.side_effect = [{"start": "original"}, None]
    with pytest.raises(HTTPException) as e:
        await m.book_test(db, spa.id, owner.id, d.id, m.now_utc()+timedelta(days=1), gateway)
    assert e.value.status_code == 409
    gateway.create.assert_not_called()
    assert d.booking is None


@pytest.mark.asyncio
async def test_no_booking_confirmation_without_provider_success(data):
    db, spa, owner, master, customer = data
    c = await campaign(data)
    await m.execute_test(db, spa.id, owner.id, c.id)
    d = (await db.execute(select(CaraDelivery))).scalar_one()
    await m.reply_test(db, spa.id, owner.id, d.id, "YES")
    gateway = AsyncMock()
    gateway.available.return_value = {"provider": "test"}
    gateway.create.return_value = {}
    with pytest.raises(HTTPException) as e:
        await m.book_test(db, spa.id, owner.id, d.id, m.now_utc()+timedelta(days=1), gateway)
    assert e.value.status_code == 502
    assert d.booking is None


@pytest.mark.asyncio
async def test_live_execution_hard_disabled(data):
    db, spa, owner, master, customer = data
    customer.extra_metadata = dict(customer.extra_metadata, is_test_customer=False)
    await db.commit()
    c = await m.prepare(db, spa.id, owner.id, "90-day reactivation", False, None)
    await m.approve(db, spa.id, master.id, c.id, c.proposal_hash)
    with pytest.raises(HTTPException) as e:
        await m.execute_test(db, spa.id, owner.id, c.id)
    assert e.value.status_code == 403


def test_no_invented_history_or_balances():
    c = Contact(id=uuid.uuid4(), first_name="Guest", extra_metadata={"marketing_sms_opt_in": True})
    result = m.insight(c, [], m.now_utc())
    assert not result["eligible"] and not result["favorite_services"]
    assert result["gift_card_balance"] is None and result["membership_benefits"] is None


def test_price_range_is_not_quoted():
    spa = SpaAccount(services=[{"name": "Massage", "duration_minutes": 60, "price": "$90-$120"}])
    assert m.menu_snapshot(spa) == []


@pytest.mark.asyncio
async def test_spa_owner_cannot_approve(data):
    db, spa, owner, master, customer = data
    c = await m.prepare(db, spa.id, owner.id, "90-day reactivation", True, None)
    with pytest.raises(HTTPException) as error:
        await m.approve(db, spa.id, owner.id, c.id, c.proposal_hash)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_repeat_campaign_cooldown(data):
    db, spa, owner, master, customer = data
    c = await campaign(data)
    await m.execute_test(db, spa.id, owner.id, c.id)
    second = await campaign(data)
    await m.execute_test(db, spa.id, owner.id, second.id)
    assert (await m.results(db, spa.id, second))["messages_sent_test"] == 0


@pytest.mark.asyncio
async def test_future_schedule_not_run_early(data):
    db, spa, owner, master, customer = data
    c = await m.prepare(db, spa.id, owner.id, "90-day reactivation", True, m.now_utc()+timedelta(days=1))
    await m.approve(db, spa.id, master.id, c.id, c.proposal_hash)
    with pytest.raises(HTTPException) as error:
        await m.execute_test(db, spa.id, owner.id, c.id)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_stop_prevents_booking(data):
    db, spa, owner, master, customer = data
    c = await campaign(data)
    await m.execute_test(db, spa.id, owner.id, c.id)
    d = (await db.execute(select(CaraDelivery))).scalar_one()
    await m.reply_test(db, spa.id, owner.id, d.id, "STOP")
    with pytest.raises(HTTPException) as error:
        await m.book_test(db, spa.id, owner.id, d.id, m.now_utc()+timedelta(days=1))
    assert error.value.status_code == 403
    assert d.booking is None


def test_upcoming_and_past_uncompleted_appointments_exclude():
    now = m.now_utc()
    c = Contact(id=uuid.uuid4(), first_name="Guest", extra_metadata={"marketing_sms_opt_in": True})
    old = Appointment(contact_id=c.id, title="Massage", status=AppointmentStatus.SCHEDULED, start_time=now-timedelta(days=120), end_time=now-timedelta(days=120)+timedelta(hours=1))
    assert not m.insight(c, [old], now)["eligible"]
    old.status = AppointmentStatus.COMPLETED
    future = Appointment(contact_id=c.id, title="Massage", status=AppointmentStatus.CONFIRMED, start_time=now+timedelta(days=1), end_time=now+timedelta(days=1,hours=1))
    assert not m.insight(c, [old, future], now)["eligible"]


@pytest.mark.parametrize("role", [UserRole.SPA_ADMIN, UserRole.SPA_STAFF])
def test_master_inbox_api_restricted(client, principal, role):
    from conftest import make_user
    principal.user = make_user(role, tenant_id=uuid.uuid4())
    assert client.get("/api/v1/cara/approvals").status_code == 403


def test_staff_cannot_use_owner_assistant(client, principal):
    from conftest import make_user
    principal.user = make_user(UserRole.SPA_STAFF, tenant_id=uuid.uuid4())
    assert client.post("/api/v1/cara/ask", json={"message": "90-day reactivation"}).status_code == 403


@pytest.mark.asyncio
async def test_owner_interpretation_never_silently_executes(data):
    db, spa, owner, master, customer = data
    result = await m.interpret(db, spa.id, "Do not discount Saturday appointments")
    assert result["kind"] == "proposed_preference"
    assert result["preferences"]["discount_excluded_weekdays"] == ["sat"]
    assert (await m.preferences(db, spa.id))["discount_excluded_weekdays"] == []
    assert (await m.interpret(db, spa.id, "Fill our Thursday afternoon openings"))["supported"] is False
    assert (await m.interpret(db, spa.id, "Send it now"))["authorized"] is False
    assert (await m.interpret(db, spa.id, "What are our hours?"))["kind"] == "question"
    assert (await m.interpret(db, spa.id, "patient medication history"))["sensitive_health_request"] is True
    assert (await m.interpret(db, spa.id, "my card number 4111111111111111"))["blocked"] is True


@pytest.mark.asyncio
async def test_ambiguous_promotion_requires_clarification(data):
    db, spa, owner, master, customer = data
    spa.services = [{"name": "European Facial"}, {"name": "Hydro Facial"}]
    result = await m.interpret(db, spa.id, "Promote facials this month")
    assert result["kind"] == "clarification" and len(result["options"]) == 2


@pytest.mark.asyncio
async def test_shared_pipeline_full_booking_and_confirmation(data):
    from app.services import campaign_booking as pipeline
    db, spa, owner, master, customer = data
    c = await campaign(data)
    await m.execute_test(db, spa.id, owner.id, c.id)
    d = (await db.execute(select(CaraDelivery))).scalar_one()
    await m.reply_test(db, spa.id, owner.id, d.id, "YES")
    selected = await pipeline.stage(db, spa.id, owner.id, d.id, future_open_time(), "Test Alice")
    assert selected["status"] == "awaiting_customer_confirmation"
    assert not (await db.execute(select(Appointment).where(Appointment.status == AppointmentStatus.SCHEDULED))).first()
    with pytest.raises(HTTPException):
        await pipeline.confirm(db, spa.id, owner.id, d.id, selected["fingerprint"], "Actually make it tomorrow")
    booked = await pipeline.confirm(db, spa.id, owner.id, d.id, selected["fingerprint"], "YES")
    assert booked["provider"] == "test_pipeline" and booked["appointment_id"] and booked["simulated"]
    repeated = await pipeline.confirm(db, spa.id, owner.id, d.id, selected["fingerprint"], "YES")
    assert repeated == booked
    rows = list((await db.execute(select(Appointment).where(Appointment.status == AppointmentStatus.SCHEDULED))).scalars())
    assert len(rows) == 1
    assert (await m.results(db, spa.id, c))["appointments_booked_test"] == 1


@pytest.mark.asyncio
async def test_shared_pipeline_disabled_in_production(data, monkeypatch):
    from app.services import campaign_booking as pipeline
    monkeypatch.setattr(pipeline.settings, "APP_ENV", "production")
    with pytest.raises(HTTPException) as error:
        pipeline.development_only()
    assert error.value.status_code == 403


@pytest.mark.parametrize("environment", ["production", " Production ", "prod"])
def test_railway_production_identity_blocks_test_pipeline_with_default_app_env(monkeypatch, environment):
    from app.services import campaign_booking as pipeline
    monkeypatch.setattr(pipeline.settings, "APP_ENV", "development")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", environment)
    with pytest.raises(HTTPException) as error:
        pipeline.development_only()
    assert error.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["execute_test", "reply_test", "book_test"])
async def test_legacy_test_mutations_reject_production_before_database_access(monkeypatch, action):
    from app.services import campaign_booking as pipeline
    monkeypatch.setattr(pipeline.settings, "APP_ENV", "development")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    args = [None, uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    if action == "reply_test":
        args.append("YES")
    elif action == "book_test":
        args.append(m.now_utc())
    with pytest.raises(HTTPException) as error:
        await getattr(m, action)(*args)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_integration_status_reports_stubs_and_no_secrets(data):
    from app.models import BookingProvider
    from app.services.booking_config import encrypt_config, validate_config
    db, spa, owner, master, customer = data
    spa.booking_provider = BookingProvider.MANGOMINT
    spa.booking_config = encrypt_config({"api_key": "never-display-this", "location_id": "test"})
    status = await m.integration_report(db, spa.id)
    assert not status["adapter_implemented"] and status["bookings_authority"] is None
    assert "never-display-this" not in str(status)
    spa.booking_provider = BookingProvider.SQUARE
    spa.booking_config = encrypt_config(validate_config(BookingProvider.SQUARE, {"access_token": "never-display-this", "location_id": "test", "environment": "sandbox"}))
    status = await m.integration_report(db, spa.id)
    assert status["square_environment"] == "sandbox" and status["secure_card_supported"]
    assert not status["live_connection_checked"]
    assert "never-display-this" not in str(status)
    with pytest.raises(ValueError):
        validate_config(BookingProvider.SQUARE, {"environment": "unsafe-endpoint"})


def test_phone_history_redacts_payment_and_flags_health():
    from app.services.call_state import CallSession
    session = CallSession("test", "inbound", "+19995550100", "test-local")
    session.add_turn("user", "My card number is 4111111111111111, cvv: 123")
    session.add_turn("user", "I have a medication question")
    assert "4111111111111111" not in str(session.to_dict())
    assert "cvv: 123" not in session.transcript_text
    assert session.entities["sensitive_health_request"] is True
    session.history.append({"role": "user", "content": "4111111111111111"})
    assert "4111111111111111" not in str(session.to_dict())


@pytest.mark.asyncio
async def test_cancellation_terms_are_specific_to_business():
    from app.services.spa_facts import lookup_spa_facts
    from tests.conftest import make_spa
    policy = {"card_required": True, "collection_mode": "secure_sms_link"}
    spa = make_spa(payment_policy=policy, booking_policies={"late_cancellation_fee": "$39", "cancellation_cutoff": "24 hours"})
    other = make_spa(payment_policy=policy, booking_policies={"cancellation_cutoff": "48 hours"})
    first = await lookup_spa_facts(spa, topic="payment")
    second = await lookup_spa_facts(other, topic="payment")
    assert first["booking_policy"]["configured"]["late_cancellation_fee"] == "$39"
    assert "$39" not in str(second) and "24-hour cancellation policy" not in second["message"]
    assert second["booking_policy"]["configured"]["cancellation_cutoff"] == "48 hours"


@pytest.mark.asyncio
async def test_platform_admin_is_not_business_master(data):
    db, spa, owner, master, customer = data
    platform = User(id=uuid.uuid4(), email="platform@example.test", hashed_password="test", role=UserRole.SUPER_ADMIN)
    db.add(platform); await db.commit()
    c = await m.prepare(db, spa.id, owner.id, "90-day reactivation", True, None)
    with pytest.raises(HTTPException) as error:
        await m.approve(db, spa.id, platform.id, c.id, c.proposal_hash)
    assert error.value.status_code == 403
    assert not m.business_master(master, uuid.uuid4())


@pytest.mark.asyncio
async def test_revoked_business_master_invalidates_execution(data):
    db, spa, owner, master, customer = data
    c = await campaign(data)
    master.is_business_master = False
    await db.commit()
    with pytest.raises(HTTPException) as error:
        await m.execute_test(db, spa.id, owner.id, c.id)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_entire_flow_through_http_api(data):
    import httpx
    from app.main import app
    from app.api.deps import get_current_user, get_db
    db, spa, owner, master, customer = data
    identity = [owner]
    async def principal():
        return identity[0]
    async def session():
        yield db
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = principal
    app.dependency_overrides[get_db] = session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-Tenant-Id": str(spa.id)}) as client:
            response = await client.post("/api/v1/cara/ask", json={"message": "Prepare 90-day reactivation"})
            assert response.status_code == 200, response.text
            c = response.json(); path = "/api/v1/cara/campaigns/" + c["id"]
            assert (await client.post(path+"/approve", json={"expected_hash": c["proposal_hash"]})).status_code == 403
            identity[0] = master
            assert (await client.get("/api/v1/cara/approvals")).status_code == 200
            assert (await client.post(path+"/approve", json={"expected_hash": c["proposal_hash"]})).status_code == 200
            identity[0] = owner
            assert (await client.post(path+"/execute-test")).status_code == 200
            r = (await client.get(path+"/results")).json()
            delivery = "/api/v1/cara/deliveries/" + r["deliveries"][0]["id"]
            assert (await client.post(delivery+"/reply-test", json={"message": "YES"})).status_code == 200
            staged = await client.post(delivery+"/stage-test-booking", json={"start": future_open_time().isoformat(), "customer_name": "Test Alice"})
            assert staged.status_code == 200, staged.text
            booked = await client.post(delivery+"/confirm-test-booking", json={"expected_fingerprint": staged.json()["fingerprint"], "confirmation": "YES"})
            assert booked.status_code == 200, booked.text
            assert booked.json()["simulated"] is True
            assert (await client.get(path+"/results")).json()["appointments_booked_test"] == 1
    finally:
        app.dependency_overrides.clear(); app.dependency_overrides.update(previous)


def test_production_fixture_creation_is_blocked(client, principal, monkeypatch):
    from tests.conftest import make_user
    from app.core.config import settings
    principal.user = make_user(UserRole.SPA_ADMIN, tenant_id=uuid.uuid4())
    monkeypatch.setattr(settings, "APP_ENV", "production")
    assert client.post("/api/v1/cara/test-customers").status_code == 403


@pytest.mark.asyncio
async def test_only_platform_can_assign_master_to_selected_business(data):
    import httpx
    from app.main import app
    from app.api.deps import get_current_user, get_db
    db, spa, owner, master, customer = data
    platform = User(id=uuid.uuid4(), email="assignment@example.test", hashed_password="test", role=UserRole.SUPER_ADMIN)
    db.add(platform); await db.commit()
    identity = [owner]
    async def principal():
        return identity[0]
    async def session():
        yield db
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = principal
    app.dependency_overrides[get_db] = session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-Tenant-Id": str(spa.id)}) as client:
            url = "/api/v1/cara/master-users/" + str(owner.id)
            assert (await client.put(url, json={"is_business_master": True})).status_code == 403
            identity[0] = platform
            assert (await client.put(url, json={"is_business_master": True})).status_code == 200
            assert owner.is_business_master
            assert (await client.put("/api/v1/cara/master-users/"+str(platform.id), json={"is_business_master": True})).status_code == 422
            assert (await client.put(url, json={"is_business_master": False})).status_code == 200
            assert not owner.is_business_master
    finally:
        app.dependency_overrides.clear(); app.dependency_overrides.update(previous)


@pytest.mark.asyncio
async def test_shared_pipeline_rejects_unqualified_staff(data):
    from app.services import campaign_booking as pipeline
    db, spa, owner, master, customer = data
    spa.staff = [{"name": "Riley", "services": ["Swedish Massage"]}, {"name": "Morgan", "services": ["Facial"]}]
    await db.commit()
    c = await campaign(data)
    await m.execute_test(db, spa.id, owner.id, c.id)
    d = (await db.execute(select(CaraDelivery))).scalar_one()
    await m.reply_test(db, spa.id, owner.id, d.id, "YES")
    with pytest.raises(HTTPException) as error:
        await pipeline.stage(db, spa.id, owner.id, d.id, m.now_utc()+timedelta(days=2), "Test Alice", "Morgan")
    assert error.value.status_code == 409 and d.booking_session is None
