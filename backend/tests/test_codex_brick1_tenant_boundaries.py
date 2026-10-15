"""Real SQLite tenant/customer queries and real Square adapters, fake HTTP only."""
import uuid
from sqlalchemy import select
from app.models import Appointment, BookingProvider, SpaAccount
from app.services import appointment_booking_service as booking
from app.services.booking_state import arm_verified_proposal, get_draft, save_draft
from app.services.booking_adapters.providers.square import SquareAdapter
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent
from tests.test_cara_native_double_booking import rubyz_db
from tests.square_world import FakeSquare, THU_2PM_UTC, local


async def setup_tenants(rubyz_db, monkeypatch):
    db, first = rubyz_db
    first.name = "Independent Cleaning"
    first.booking_provider = BookingProvider.SQUARE
    first.booking_policies = {}
    first.booking_config = {"access_token": "fake-cleaning-token", "location_id": "loc_123"}
    first.services = [{"name": "House Cleaning", "duration_minutes": 60,
                       "square_variation_id": "var_swedish_60", "square_variation_version": 3}]
    second = SpaAccount(id=uuid.uuid4(), name="Independent Roofing", timezone=first.timezone,
                        twilio_phone_number="+15550002000", booking_provider=BookingProvider.SQUARE,
                        booking_config={"access_token": "fake-roofing-token", "location_id": "loc_123"},
                        booking_policies={}, business_hours=first.business_hours,
                        services=[{"name": "Roof Estimate", "duration_minutes": 60,
                                   "square_variation_id": "var_facial_60", "square_variation_version": 5}],
                        staff=[])
    db.add(second)
    await db.commit()
    squares = {s.booking_config["access_token"]: FakeSquare() for s in [first, second]}
    for square in squares.values():
        square.open_all(THU_2PM_UTC)
    async def request(adapter, method, path, *, json=None):
        return await squares[adapter.config["access_token"]](method, path, json=json)
    monkeypatch.setattr(SquareAdapter, "_request", request)
    return db, first, second, squares


def session_for(spa, suffix, phone="+15550000002"):
    return CallSession("CA-sql-"+suffix, "inbound", phone, spa.twilio_phone_number,
                       tenant_id=str(spa.id), timezone=spa.timezone)


async def stage(db, spa, session):
    service = spa.services[0]["name"]
    result = await booking.stage_booking(db, session, AppointmentIntent(
        intent="schedule", confidence=1, caller_name="Ada Lovelace",
        service_description=service, requested_start_iso=local(THU_2PM_UTC)))
    assert result.outcome is booking.BookingOutcome.DRAFT, result.message


async def book(db, spa, session):
    await stage(db, spa, session)
    arm_verified_proposal(session)
    result = await booking.confirm_booking(db, session)
    assert result.outcome is booking.BookingOutcome.BOOKED, result.message
    return result.appointment


async def test_interleaved_tenants_use_own_catalog_customer_credentials_and_calendar(rubyz_db, monkeypatch):
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    sa, sb = session_for(a, "a"), session_for(b, "b")
    # Both calls are open at once, same caller number, same UTC slot.
    await stage(db, a, sa)
    await stage(db, b, sb)
    before_a = dict(sa.entities)
    arm_verified_proposal(sb)
    rb = await booking.confirm_booking(db, sb)
    assert rb.outcome is booking.BookingOutcome.BOOKED, rb.message
    assert sa.entities == before_a
    arm_verified_proposal(sa)
    ra = await booking.confirm_booking(db, sa)
    assert ra.outcome is booking.BookingOutcome.BOOKED, ra.message
    rows = list((await db.execute(select(Appointment))).scalars())
    assert len(rows) == 2
    assert ra.appointment.contact_id != rb.appointment.contact_id
    assert ra.appointment.tenant_id == a.id and rb.appointment.tenant_id == b.id
    assert len(squares["fake-cleaning-token"].creates) == len(squares["fake-roofing-token"].creates) == 1
    assert squares["fake-cleaning-token"].active()[0]["appointment_segments"][0]["service_variation_id"] == "var_swedish_60"
    assert squares["fake-roofing-token"].active()[0]["appointment_segments"][0]["service_variation_id"] == "var_facial_60"
    assert [r.id for r in await booking.lookup_upcoming_appointments(db, sa)] == [ra.appointment.id]
    assert [r.id for r in await booking.lookup_upcoming_appointments(db, sb)] == [rb.appointment.id]


async def test_foreign_tenant_appointment_id_cannot_cancel(rubyz_db, monkeypatch):
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    victim = await book(db, a, session_for(a, "victim"))
    attacker = session_for(b, "attacker")
    # Even a contaminated offered-ID list must not defeat the SQL scope.
    draft = get_draft(attacker)
    draft.offered_appointment_ids = [str(victim.id)]
    save_draft(attacker, draft)
    result = await booking.cancel_booking(db, attacker, appointment_id=str(victim.id))
    assert result.outcome is not booking.BookingOutcome.CANCELLED
    assert len(squares["fake-cleaning-token"].active()) == 1
    assert not squares["fake-roofing-token"].creates
    assert not any(path.endswith("/cancel") for square in squares.values() for _, path, _ in square.calls)


async def test_wrong_customer_lookup_in_same_business_discloses_no_appointment(rubyz_db, monkeypatch):
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    await book(db, a, session_for(a, "victim"))
    other = session_for(a, "other", phone="+15550009999")
    assert await booking.lookup_upcoming_appointments(db, other) == []


async def test_unoffered_same_tenant_id_cannot_cancel(rubyz_db, monkeypatch):
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    victim = await book(db, a, session_for(a, "victim"))
    other = session_for(a, "other", phone="+15550009999")
    result = await booking.cancel_booking(db, other, appointment_id=str(victim.id))
    assert result.outcome is booking.BookingOutcome.MISSING_INFO
    assert len(squares["fake-cleaning-token"].active()) == 1


async def test_appointment_update_route_must_keep_square_and_local_booking_consistent(rubyz_db, monkeypatch):
    from datetime import timedelta
    from app.api.v1.appointments import update_appointment
    from app.schemas import AppointmentUpdate
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    session = session_for(a, "victim")
    row = await book(db, a, session)
    await update_appointment(row.id, AppointmentUpdate(start_time=THU_2PM_UTC + timedelta(hours=2),
                             end_time=THU_2PM_UTC + timedelta(hours=3)), scope=session.scope, db=db)
    assert squares["fake-cleaning-token"].active()[0]["start_at"] == "2026-10-22T21:00:00Z", (
        "LOCAL ROUTE CHANGED BOOKING WITHOUT SQUARE UPDATE", row.start_time,
        squares["fake-cleaning-token"].active())


async def test_appointment_delete_route_must_keep_square_and_local_booking_consistent(rubyz_db, monkeypatch):
    from app.api.v1.appointments import cancel_appointment
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    session = session_for(a, "victim")
    row = await book(db, a, session)
    await cancel_appointment(row.id, scope=session.scope, db=db)
    assert not squares["fake-cleaning-token"].active(), (
        "LOCAL ROUTE CANCELLED ROW BUT SQUARE REMAINS BOOKED", row.status,
        squares["fake-cleaning-token"].active())
