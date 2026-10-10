import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.services.booking_adapters.base import BookingContext, BookingProviderError, AvailabilityVerdict
from app.services.booking_adapters.spa_router import SpaBookingAdapter
from app.services.visit_booking import VisitPolicy, validate_sequence
from app.services.grok_service import AppointmentIntent
from tests.test_booking_intent_state import world, _confirm


def instant(hour=14, minute=0):
    return datetime(2027, 10, 8, hour, minute, tzinfo=ZoneInfo('America/Chicago'))


@pytest.fixture
def visit():
    services = [dict(name='European facial', duration_minutes=60, square_variation_id='facial', square_variation_version=1),
                dict(name='Deep tissue massage', duration_minutes=90, square_variation_id='massage', square_variation_version=2),
                dict(name='Brow wax', duration_minutes=15, square_variation_id='wax', square_variation_version=3)]
    staff = [dict(name='Alice', provider_id='alice', services=['European facial', 'Brow wax']),
             dict(name='Bob', provider_id='bob', services=['Deep tissue massage'])]
    spa = SimpleNamespace(name='Test Spa', services=services, staff=staff, timezone='America/Chicago',
                          business_hours={'fri': [{'open': '09:00', 'close': '18:00'}]}, booking_policies={})
    adapter = SpaBookingAdapter.__new__(SpaBookingAdapter)
    adapter.spa = spa
    adapter.default_duration_minutes = 60
    adapter.provider = 'square'
    provider = SimpleNamespace(provider='square', location_id='loc', timezone_name=spa.timezone)
    adapter.delegate = provider
    provider._parse_square_datetime = lambda s: datetime.fromisoformat(s.replace('Z', '+00:00'))
    provider._square_datetime = lambda dt: dt.astimezone(timezone.utc).isoformat()
    provider._idempotency_key = lambda *args: '|'.join(str(a) for a in args)
    provider._location = AsyncMock(return_value={'timezone': spa.timezone})
    provider.booking_timezone_name = AsyncMock(return_value=spa.timezone)
    provider._get_or_create_customer = AsyncMock(return_value='customer')
    provider._resolve_preferred_team_member = AsyncMock(side_effect=lambda name: name.lower())
    async def resolve(ctx):
        return {'id': ctx.service_variation_id, 'version': ctx.service_variation_version}
    provider._resolve_service_variation = resolve
    provider.cancel_booking = AsyncMock()
    provider.check_availability = AsyncMock(return_value=AvailabilityVerdict.ok())
    provider.create_booking = AsyncMock()
    state = dict(start=instant(), available=True, returned_count=None, timeout=0, calls=[], wrong_staff=False)
    async def request(method, path, **kwargs):
        state['calls'].append((method, path, copy.deepcopy(kwargs)))
        if path.startswith('/v2/catalog/object/'):
            s = next(s for s in services if s['square_variation_id'] == path.rsplit('/', 1)[1])
            return {'object': {'id': s['square_variation_id'], 'version': s['square_variation_version'],
                              'item_variation_data': {'available_for_booking': True, 'service_duration': s['duration_minutes'] * 60000}}}
        if path.endswith('/availability/search'):
            if not state['available']:
                return {'availabilities': []}
            filters = kwargs['json']['query']['filter']['segment_filters']
            segments = []
            for f in filters:
                s = next(s for s in services if s['square_variation_id'] == f['service_variation_id'])
                segments.append(dict(duration_minutes=s['duration_minutes'], service_variation_id=f['service_variation_id'],
                    service_variation_version=s['square_variation_version'], team_member_id='bob' if f['service_variation_id'] == 'massage' else 'alice',
                    intermission_minutes=state.get('gap', 0), resource_ids=state.get('resources', [])))
            if state['wrong_staff']:
                segments[0]['team_member_id'] = 'bob'
            return {'availabilities': [{'start_at': state['start'].isoformat(), 'location_id': 'loc', 'appointment_segments': segments}]}
        if method == 'GET' and path == '/v2/bookings':
            return {'bookings': []}
        assert method == 'POST' and path == '/v2/bookings'
        if state['timeout']:
            state['timeout'] -= 1
            raise BookingProviderError('transport timeout', retryable=True)
        record = copy.deepcopy(kwargs['json']['booking'])
        record.update(id='visit-1', status='ACCEPTED')
        if state['returned_count'] is not None:
            record['appointment_segments'] = record['appointment_segments'][:state['returned_count']]
        if state.get('missing_start'):
            record.pop('start_at')
        return {'booking': record}
    provider._request = request
    ctx = BookingContext(start=instant(), end=instant()+timedelta(minutes=150), title='Visit', customer_phone='+15550000001',
        service_description='European facial + Deep tissue massage', booking_reference='intent-1')
    return adapter, ctx, state


async def pinned(visit):
    router, ctx, state = visit
    verdict = await router.check_availability(ctx)
    assert verdict.available, verdict.reason
    return replace(ctx, selected_slot=verdict.slot)


async def test_two_treatments_different_staff_one_atomic_write(visit):
    router, ctx, state = visit
    ctx = await pinned(visit)
    assert ctx.selected_slot['duration_minutes'] == 150
    assert [s['team_member_id'] for s in ctx.selected_slot['visit_segments']] == ['alice', 'bob']
    result = await router.create_booking(ctx)
    assert result.external_id == 'visit-1'
    writes = [kw['json'] for method, path, kw in state['calls'] if method == 'POST' and path == '/v2/bookings']
    assert len(writes) == 1 and len(writes[0]['booking']['appointment_segments']) == 2
    assert router.duration_for_service(ctx.service_description) == 150


@pytest.mark.parametrize('hour,minute,available', [(17,0,False),(15,30,True),(15,31,False)])
async def test_entire_visit_must_fit_closing(visit, hour, minute, available):
    router, ctx, state = visit
    state['start'] = instant(hour, minute)
    verdict = await router.check_availability(replace(ctx, start=state['start']))
    assert verdict.available is available


async def test_three_services(visit):
    router, ctx, state = visit
    ctx = replace(ctx, service_description=ctx.service_description+' + Brow wax')
    verdict = await router.check_availability(ctx)
    assert verdict.available and verdict.slot['duration_minutes'] == 165
    result = await router.create_booking(replace(ctx, selected_slot=verdict.slot))
    assert result.external_id


async def test_single_service_preserves_existing_adapter(visit):
    router, ctx, _ = visit
    await router.check_availability(replace(ctx, service_description='European facial'))
    router.delegate.check_availability.assert_awaited_once()


@pytest.mark.parametrize('change', ['room', 'staff', 'staff_hours', 'holiday', 'buffer', 'preparation'])
async def test_unverified_requirements_block_the_whole_visit(visit, change):
    router, ctx, state = visit
    if change == 'room': router.spa.services[0]['resource_ids'] = ['room-1']
    if change == 'staff': state['wrong_staff'] = True
    if change == 'staff_hours': router.spa.staff[1]['hours'] = {'fri': [{'open':'09:00','close':'15:00'}]}
    if change == 'holiday': router.spa.booking_policies = {'visit': {'special_hours': {'2027-10-08': []}}}
    if change == 'buffer': router.spa.services[0]['cleanup_buffer_minutes'] = 10
    if change == 'preparation': router.spa.services[0]['preparation_buffer_minutes'] = 10
    assert not (await router.check_availability(ctx)).available


async def test_verified_buffers_and_resources_count_toward_closing(visit):
    router, ctx, state = visit
    router.spa.services[0].update(cleanup_buffer_minutes=10, resource_ids=['room-1'])
    state.update(gap=10, resources=['room-1'], start=instant(15,20))
    assert not (await router.check_availability(replace(ctx, start=state['start']))).available
    state['start'] = instant(15,10)
    result = await router.check_availability(replace(ctx, start=state['start']))
    assert result.available and result.slot['duration_minutes'] == 170


async def test_cancelled_slot_can_be_queried_again(visit):
    router, ctx, state = visit
    state['available'] = False
    assert not (await router.check_availability(ctx)).available
    state['available'] = True  # provider now reports cancelled slot free
    assert (await router.check_availability(ctx)).available


async def test_final_recheck_prevents_stale_write(visit):
    router, _, state = visit
    ctx = await pinned(visit)
    state['available'] = False
    with pytest.raises(BookingProviderError, match='no longer available'):
        await router.create_booking(ctx)
    assert not any(path == '/v2/bookings' for _, path, _ in state['calls'])


async def test_partial_provider_result_is_compensated(visit):
    router, _, state = visit
    ctx = await pinned(visit)
    state['returned_count'] = 1
    with pytest.raises(BookingProviderError) as caught:
        await router.create_booking(ctx)
    assert caught.value.code == 'VISIT_ROLLED_BACK'
    router.delegate.cancel_booking.assert_awaited_once()


async def test_failed_compensation_is_flagged(visit):
    router, _, state = visit
    ctx = await pinned(visit)
    state['returned_count'] = 1
    router.delegate.cancel_booking.side_effect = BookingProviderError('unavailable')
    with pytest.raises(BookingProviderError) as caught:
        await router.create_booking(ctx)
    assert caught.value.code == 'PARTIAL_BOOKING_UNRESOLVED'


async def test_malformed_provider_success_cannot_bypass_compensation(visit):
    router, _, state = visit
    ctx = await pinned(visit)
    state['missing_start'] = True
    with pytest.raises(BookingProviderError): await router.create_booking(ctx)
    router.delegate.cancel_booking.assert_awaited_once()


@pytest.mark.parametrize('timeouts', [1,2])
async def test_timeout_retries_identical_idempotent_payload(visit, timeouts):
    router, _, state = visit
    ctx = await pinned(visit)
    state['timeout'] = timeouts
    if timeouts == 2:
        with pytest.raises(BookingProviderError) as caught: await router.create_booking(ctx)
        assert caught.value.code == 'BOOKING_OUTCOME_UNKNOWN'
    else:
        assert (await router.create_booking(ctx)).external_id
    writes = [kw['json'] for method, path, kw in state['calls'] if method == 'POST' and path == '/v2/bookings']
    assert len(writes) == 2 and writes[0] == writes[1]


async def test_reordering_requires_business_opt_in(visit):
    router, ctx, state = visit
    await router.check_availability(ctx)
    searches = lambda: [kw for _, p, kw in state['calls'] if p.endswith('/availability/search')]
    assert len(searches()) == 1
    router.spa.booking_policies = {'visit': {'allow_reorder': True}}
    state['calls'].clear()
    await router.check_availability(ctx)
    assert len(searches()) == 2


def test_structured_intent_preserves_every_treatment():
    intent = AppointmentIntent(intent='schedule', service_description='facial', requested_services=['European facial', '90 minute Deep tissue massage'])
    assert intent.service_description == 'European facial + 90 minute Deep tissue massage'


def test_invalid_special_hours_rejected():
    with pytest.raises(ValueError): VisitPolicy(special_hours={'2027-10-08': [{'open':'18:00','close':'09:00'}]})


async def test_real_booking_pipeline_saves_complete_visit_once(world, visit, monkeypatch):
    from app.services import appointment_booking_service as booking
    from app.models.appointment import CardStatus
    router, ctx, state = visit
    router.calendar_label = 'test Square calendar'
    router.default_title = 'Spa visit'
    world['spa'].services = router.spa.services
    world['spa'].staff = router.spa.staff
    world['spa'].timezone = router.spa.timezone
    world['spa'].business_hours = router.spa.business_hours
    monkeypatch.setattr(booking, 'get_booking_adapter', lambda **kw: router)
    monkeypatch.setattr(booking, 'upsert_external_customer_link', AsyncMock(return_value=None))
    monkeypatch.setattr(booking, 'card_status_for_booked_appointment', AsyncMock(return_value=CardStatus.NOT_REQUIRED))
    intent = AppointmentIntent(intent='schedule', requested_start_iso=ctx.start.isoformat(),
        requested_services=['European facial', 'Deep tissue massage'], caller_name='Test Customer')
    result = await booking.stage_booking(world['db'], world['session'], intent)
    assert result.outcome == booking.BookingOutcome.DRAFT, result.message
    assert not world['store'].live
    result = await _confirm(world['db'], world['session'])
    assert result.outcome == booking.BookingOutcome.BOOKED, result.message
    assert len(world['store'].live) == 1
    row = world['store'].live[0]
    assert row.end_time - row.start_time == timedelta(minutes=150)
    assert 'European facial' in row.description and 'Deep tissue massage' in row.description
    result = await _confirm(world['db'], world['session'])
    assert result.outcome == booking.BookingOutcome.BOOKED
    assert len([p for _, p, _ in state['calls'] if p == '/v2/bookings']) == 1


async def test_unknown_outcome_blocks_further_session_writes(world):
    from app.services import appointment_booking_service as booking
    world['session'].entities['booking_reconciliation_required'] = {'code': 'BOOKING_OUTCOME_UNKNOWN'}
    result = await booking.confirm_booking(world['db'], world['session'])
    assert result.outcome == booking.BookingOutcome.ERROR
    assert not world['adapter'].created


async def test_after_hours_requires_explicit_permission_and_window(visit):
    router, ctx, state = visit
    state['start'] = instant(17)
    ctx = replace(ctx, start=state['start'])
    router.spa.booking_policies = {'visit': {'allow_after_hours': True}}
    assert not (await router.check_availability(ctx)).available
    router.spa.booking_policies['visit']['after_hours'] = {'fri': [{'open':'17:00','close':'20:00'}]}
    assert (await router.check_availability(ctx)).available


async def test_later_staff_change_invalidates_pinned_complete_sequence(visit):
    router, _, state = visit
    ctx = await pinned(visit)
    router.spa.staff[1]['hours'] = {'fri': [{'open':'09:00','close':'15:00'}]}
    with pytest.raises(BookingProviderError): await router.create_booking(ctx)
    assert not any(p == '/v2/bookings' for _, p, _ in state['calls'])


async def test_late_request_returns_verified_earlier_sequence(visit):
    router, ctx, state = visit
    state['start'] = instant(15,30)
    result = await router.check_availability(replace(ctx, start=instant(17)))
    assert not result.available
    assert datetime.fromisoformat(result.alternatives[0]['start']) == state['start']
    assert result.alternatives[0]['duration_minutes'] == 150


async def test_single_service_with_resource_requirement_is_not_bypassed(visit):
    router, ctx, state = visit
    router.spa.services[0]['resource_ids'] = ['facial-room']
    result = await router.check_availability(replace(ctx, service_description='European facial'))
    assert not result.available
    router.delegate.check_availability.assert_not_awaited()


def test_proposal_fingerprint_binds_second_provider(visit):
    from app.services.booking_state import BookingDraft, proposal_fingerprint
    draft = BookingDraft()
    draft.selected_slot = {'visit_segments': [{'team_member_id':'alice'}, {'team_member_id':'bob'}]}
    before = proposal_fingerprint(draft)
    draft.selected_slot['visit_segments'][1]['team_member_id'] = 'charlie'
    assert proposal_fingerprint(draft) != before


def test_dashboard_round_trip_preserves_real_booking_rules():
    from app.schemas.spa_account import SpaAccountUpdate
    payload = {'booking_policies': {'visit': {'enabled':True, 'allow_reorder':True}},
        'services': [{'name':'Facial', 'resource_ids':['room'], 'cleanup_buffer_minutes':10}],
        'staff': [{'name':'Alice','provider_id':'alice','hours':{'fri':[{'open':'09:00','close':'18:00'}]}}]}
    result = SpaAccountUpdate.model_validate(payload).model_dump(exclude_unset=True)
    assert result['services'][0]['cleanup_buffer_minutes'] == 10
    assert result['staff'][0]['hours']['fri'][0]['close'] == '18:00'
    assert result['booking_policies']['visit']['allow_reorder']


def test_tool_schemas_carry_all_requested_services():
    from app.services.xai_realtime import CHECK_AVAILABILITY_TOOL, PROPOSE_APPOINTMENT_TOOL
    for tool in [CHECK_AVAILABILITY_TOOL, PROPOSE_APPOINTMENT_TOOL]:
        assert tool['parameters']['properties']['requested_services']['type'] == 'array'
