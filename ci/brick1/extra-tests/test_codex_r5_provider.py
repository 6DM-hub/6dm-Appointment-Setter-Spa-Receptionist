"""Independent R5 fault-injection candidates; not executed in this environment."""
import asyncio
import copy
from datetime import timedelta
import pytest
from fastapi import HTTPException
from app.services.booking_adapters.base import BookingProviderError
from app.services import appointment_booking_service as booking
from app.services.booking_state import arm_verified_proposal
from app.services.grok_service import AppointmentIntent
from app.api.v1.appointments import cancel_appointment, update_appointment
from app.schemas import AppointmentUpdate
from tests.test_cara_native_double_booking import rubyz_db
from tests.test_codex_brick1_tenant_boundaries import setup_tenants, session_for, book
from tests.test_brick1_booking_change_guard import T3, local

def writes(square):
    return [c for c in square.calls if c[0] == 'PUT' or c[1].endswith('/cancel')]

@pytest.mark.parametrize('fault', ['lost_response', 'interrupted', 'refused', 'false_success'])
async def test_dashboard_fault_then_voice_and_dashboard_retry_cannot_duplicate_write(rubyz_db, monkeypatch, fault):
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    caller = session_for(a, 'r5-fault')
    row = await book(db, a, caller)
    row_id, scope = row.id, caller.scope
    square = squares['fake-cleaning-token']
    square.open_all(T3)
    async def transport(method, route, *, json=None):
        if method == 'PUT':
            if fault == 'refused':
                square.calls.append((method, route, copy.deepcopy(json)))
                raise BookingProviderError('permission refused', code='FORBIDDEN')
            if fault == 'false_success':
                square.calls.append((method, route, copy.deepcopy(json)))
                return {'booking': {'id': 'unrelated-booking', 'status': 'ACCEPTED'}}
            result = await square(method, route, json=json)
            if fault == 'interrupted':
                raise asyncio.CancelledError('worker stopped after provider applied move')
            raise BookingProviderError('response lost after provider applied move', code='BOOKING_OUTCOME_UNKNOWN')
        return await square(method, route, json=json)
    squares['fake-cleaning-token'] = transport
    with pytest.raises((HTTPException, asyncio.CancelledError)):
        await update_appointment(row_id, AppointmentUpdate(start_time=T3, end_time=T3+timedelta(hours=1)), scope=scope, db=db)
    count = len(writes(square))
    assert count == 1, 'Fault must reach exactly one attempted provider move'
    await db.rollback()
    # A definite refusal can be retried after new consent; unknown outcomes cannot.
    if fault == 'refused':
        return
    await db.refresh(a)
    squares['fake-cleaning-token'] = square
    later = session_for(a, 'r5-later')
    later.add_turn('user', 'Move my appointment to Thursday at 3 PM instead.')
    await booking.stage_booking(db, later, AppointmentIntent(intent='reschedule', confidence=1,
        service_description=a.services[0]['name'], requested_start_iso=local(T3)))
    arm_verified_proposal(later)
    await booking.confirm_booking(db, later)
    try:
        await cancel_appointment(row_id, scope=scope, db=db)
    except HTTPException:
        pass
    assert len(writes(square)) == count, ('UNKNOWN_OUTCOME_RETRIED', fault, writes(square))

@pytest.mark.parametrize('operation', ['move', 'cancel'])
@pytest.mark.parametrize('configured', [True, False])
@pytest.mark.parametrize('error_status', ['insufficient_permissions', 'event_not_found', 'google_api_error'])
async def test_google_failure_never_mutates_local_mirror(rubyz_db, monkeypatch, operation, configured, error_status):
    from types import SimpleNamespace
    from app.models import BookingProvider
    from app.services.booking_adapters.google_calendar import GoogleCalendarAdapter
    from app.services.booking_adapters import google_calendar as gcal
    from app.services.google_calendar import GoogleCalendarError
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    caller = session_for(a, 'r5-google')
    row = await book(db, a, caller)
    row.booking_provider = BookingProvider.GOOGLE_CALENDAR.value
    row.external_booking_id = 'r5-google-event'
    await db.commit()
    before = (row.start_time, row.status)
    adapter = GoogleCalendarAdapter(calendar_id='dummy', connection=SimpleNamespace() if configured else None)
    monkeypatch.setattr(booking, 'get_booking_adapter', lambda **kw: adapter)
    calls = []
    async def reject(*args, **kwargs):
        calls.append(operation)
        raise GoogleCalendarError(error_status, 'Simulated Google failure')
    monkeypatch.setattr(gcal, 'update_event', reject)
    monkeypatch.setattr(gcal, 'delete_event', reject)
    with pytest.raises(HTTPException):
        if operation == 'move':
            await update_appointment(row.id, AppointmentUpdate(start_time=T3, end_time=T3+timedelta(hours=1)), scope=caller.scope, db=db)
        else:
            await cancel_appointment(row.id, scope=caller.scope, db=db)
    await db.refresh(row)
    assert (row.start_time, row.status) == before
    assert calls == ([operation] if configured else [])
