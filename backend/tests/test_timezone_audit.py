from datetime import datetime, timedelta, timezone
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from app.services.scheduling_time import business_zone, localize_wall_time, utc_instant, elapsed_end
from app.services.business_hours import is_open_between
from app.services import visit_booking as visits
from app.services import appointment_booking_service as booking
from app.services.booking_adapters.base import BookingProviderError
from app.services.booking_adapters.providers.square import SquareAdapter
from app.schemas.spa_account import SpaAccountUpdate
from tests.test_multi_service_visit import visit, instant
from tests.test_booking_intent_state import world


@pytest.mark.parametrize('month,offset', [(1,-6),(7,-5)])
def test_dallas_uses_dst_not_fixed_offset(month,offset,monkeypatch):
    monkeypatch.setenv('TZ','Asia/Manila')
    value=localize_wall_time(datetime(2027,month,8,15),business_zone('America/Chicago'))
    assert value.utcoffset()==timedelta(hours=offset)
    assert utc_instant(value).hour==15-offset


@pytest.mark.parametrize('wall',[datetime(2027,3,14,2,30),datetime(2027,11,7,1,30)])
def test_dst_gap_and_fold_require_clarification(wall):
    with pytest.raises(ValueError): localize_wall_time(wall,business_zone('America/Chicago'))
    assert booking._parse_dt(wall.isoformat(),business_zone('America/Chicago')) is None


def test_explicit_fall_back_instants_are_distinct():
    a=booking._parse_dt('2027-11-07T01:30:00-05:00')
    b=booking._parse_dt('2027-11-07T01:30:00-06:00')
    assert utc_instant(b)-utc_instant(a)==timedelta(hours=1)
    assert elapsed_end(a,90)==datetime(2027,11,7,8,tzinfo=timezone.utc)


@pytest.mark.parametrize('zone,hour',[('America/New_York',19),('America/Los_Angeles',22),('America/Chicago',20),('America/Phoenix',22)])
def test_each_business_has_independent_zone(zone,hour):
    local=localize_wall_time(datetime(2027,7,8,15),business_zone(zone))
    assert utc_instant(local).hour==hour


@pytest.mark.parametrize('zone',['PST','GMT-6','UTC-06:00','Not/AZone',None])
def test_invalid_business_timezone_cannot_be_saved(zone):
    with pytest.raises(ValueError): SpaAccountUpdate(timezone=zone)


def test_philippine_offset_does_not_reinterpret_dallas_hours():
    # 5 PM in Manila is 4 AM Central, not a valid Dallas 5 PM appointment.
    start=booking._parse_dt('2027-07-08T17:00:00+08:00')
    assert start.astimezone(business_zone('America/Chicago')).hour==4
    assert not is_open_between({'thu':[{'open':'09:00','close':'18:00'}]},'America/Chicago',start,elapsed_end(start,60))


@pytest.mark.parametrize('hours',[{}, {'mon':[{'open':'09:00','close':'18:00'}]}])
def test_missing_hours_never_authorize_unconfigured_day(hours):
    assert not is_open_between(hours,'America/Chicago',instant(),elapsed_end(instant(),60))


async def test_unrelated_holiday_does_not_enable_unconfigured_days(visit):
    router,ctx,state=visit
    router.spa.business_hours={}
    router.spa.booking_policies={'visit':{'special_hours':{'2027-12-25':[]}}}
    assert not (await router.check_availability(ctx)).available
    assert not (await router.list_openings(ctx,instant(9),instant(18)))


async def test_explicit_special_opening_can_authorize_that_date_only(visit):
    router,ctx,state=visit
    router.spa.business_hours={}
    router.spa.booking_policies={'visit':{'special_hours':{'2027-10-08':[{'open':'09:00','close':'18:00'}]}}}
    assert (await router.check_availability(ctx)).available


@pytest.mark.parametrize('phrase',['Face and Neck','60 minute Face and Neck','Face and Neck (60 min)','60-minute Face and Neck'])
def test_compound_catalog_name_stays_one_service(phrase):
    router=SimpleNamespace(spa=SimpleNamespace(services=[{'name':'Face and Neck','duration_minutes':60}]))
    assert visits.parts(router,phrase)==[phrase]


@pytest.mark.parametrize('code',['UNAUTHORIZED','RATE_LIMITED','TRANSPORT_ERROR','INVALID_RESPONSE'])
async def test_api_failure_is_not_an_unavailable_slot(visit,monkeypatch,code):
    router,ctx,_=visit
    monkeypatch.setattr(visits,'list_visits',AsyncMock(side_effect=BookingProviderError('provider failed',code=code)))
    with pytest.raises(BookingProviderError) as caught: await router.check_availability(ctx)
    assert caught.value.code==code


def test_naive_provider_dates_never_use_machine_timezone(monkeypatch):
    monkeypatch.setenv('TZ','Asia/Manila')
    with pytest.raises(ValueError): SquareAdapter._parse_square_datetime('2027-07-08T15:00:00')
    assert SquareAdapter._parse_square_datetime('2027-07-08T15:00:00-05:00').hour==20


async def test_provider_zone_cannot_override_business_zone(visit):
    router,ctx,_=visit
    router.delegate.timezone_name='Asia/Manila'
    with pytest.raises(BookingProviderError): await router.check_availability(ctx)


async def test_empty_hours_single_service_blocked(visit):
    router,ctx,_=visit
    router.spa.business_hours={}
    assert not (await router.check_availability(replace(ctx,service_description='European facial'))).available


def test_rest_appointment_input_requires_offset_and_normalizes_utc():
    from app.schemas.appointment import AppointmentBase
    with pytest.raises(ValueError): AppointmentBase(title='Test',start_time='2027-07-08T15:00',end_time='2027-07-08T16:00')
    model=AppointmentBase(title='Test',start_time='2027-07-08T15:00-05:00',end_time='2027-07-08T16:00-05:00')
    assert model.start_time==datetime(2027,7,8,20,tzinfo=timezone.utc)


async def test_day_lookup_failure_offers_callback_not_other_times(monkeypatch):
    from tests.test_availability_retry_repair import voice
    from app.services import xai_realtime as realtime
    v=voice()
    v._persist_session=AsyncMock()
    v._send_function_output=AsyncMock()
    v._offer_staff_callback=AsyncMock()
    v._deliver_authoritative_availability=AsyncMock()
    v._arm_availability_hold=AsyncMock()
    monkeypatch.setattr(realtime,'search_day_part',AsyncMock(side_effect=BookingProviderError('network failure')))
    await v._offer_spoken_window('test',(instant(9),instant(18)),{'service_description':'facial'})
    v._offer_staff_callback.assert_awaited_once()
    v._deliver_authoritative_availability.assert_not_awaited()


@pytest.mark.parametrize('separator',[' + ',' and ',' then ','; '])
def test_compound_name_preserved_inside_multi_service_request(separator):
    router=SimpleNamespace(spa=SimpleNamespace(services=[{'name':'Face and Neck'},{'name':'Massage'}]))
    assert visits.parts(router,'60 minute Face and Neck'+separator+'90 minute Massage')==['60 minute Face and Neck','90 minute Massage']


@pytest.mark.parametrize('payload',[[], {'errors':[{'code':'UNAUTHORIZED'}]}, {'availabilities':'bad'}, {'availabilities':[None]}])
async def test_malformed_availability_response_is_not_a_closed_slot(visit,payload):
    router,ctx,_=visit
    request=router.delegate._request
    async def malformed(method,path,**kwargs):
        if path.endswith('/availability/search'): return payload
        return await request(method,path,**kwargs)
    router.delegate._request=malformed
    with pytest.raises(BookingProviderError) as caught: await router.check_availability(ctx)
    assert caught.value.code=='INVALID_RESPONSE'


def test_fall_back_visit_duration_uses_elapsed_time_and_closing():
    start=datetime.fromisoformat('2027-11-07T01:30:00-05:00')
    end=elapsed_end(start,150)
    assert end.astimezone(business_zone('America/Chicago')).strftime('%H:%M')=='03:00'
    assert is_open_between({'sun':[{'open':'00:00','close':'03:00'}]},'America/Chicago',start,end)
    assert not is_open_between({'sun':[{'open':'00:00','close':'02:30'}]},'America/Chicago',start,end)


def test_spring_forward_visit_duration_uses_elapsed_time_and_closing():
    start=datetime.fromisoformat('2027-03-14T01:30:00-06:00')
    end=elapsed_end(start,150)
    assert end.astimezone(business_zone('America/Chicago')).strftime('%H:%M')=='05:00'
    assert not is_open_between({'sun':[{'open':'00:00','close':'04:00'}]},'America/Chicago',start,end)


async def test_single_service_day_search_refuses_provider_zone_mismatch(visit):
    router,ctx,_=visit
    router.delegate.timezone_name='Asia/Manila'
    router.delegate.list_openings=AsyncMock(return_value=[])
    with pytest.raises(BookingProviderError):
        await router.list_openings(replace(ctx,service_description='European facial'),instant(9),instant(18))


def test_naive_provider_slot_is_not_silently_assumed_utc():
    from app.services.booking_adapters.spa_router import _parse_slot_start
    assert _parse_slot_start('2027-07-08T15:00:00') is None


from tests.test_cara_manager import data

async def test_multi_service_creation_persists_in_real_local_database(data,visit,monkeypatch):
    from sqlalchemy import select
    from app.models import Appointment, AppointmentStatus
    from app.models.appointment import CardStatus
    from app.services.call_state import CallSession
    from app.services.grok_service import AppointmentIntent
    from app.services.booking_state import arm_verified_proposal
    db,spa,owner,master,customer=data
    router,ctx,state=visit
    spa.services=router.spa.services
    spa.staff=router.spa.staff
    spa.business_hours=router.spa.business_hours
    router.spa=spa
    router.calendar_label='local mocked Square'
    router.default_title='Spa visit'
    session=CallSession('local-multi-db','inbound',customer.phone_number,'test-local',tenant_id=str(spa.id),timezone=spa.timezone)
    monkeypatch.setattr(booking,'get_booking_adapter',lambda **kwargs: router)
    monkeypatch.setattr(booking,'_load_spa',AsyncMock(return_value=spa))
    monkeypatch.setattr(booking,'upsert_external_customer_link',AsyncMock(return_value=None))
    monkeypatch.setattr(booking,'card_status_for_booked_appointment',AsyncMock(return_value=CardStatus.NOT_REQUIRED))
    monkeypatch.setattr(booking,'_notify_staff_event',AsyncMock())
    request=AppointmentIntent(intent='schedule',caller_name='Test Alice',requested_start_iso=ctx.start.isoformat(),requested_services=['European facial','Deep tissue massage'])
    staged=await booking.stage_booking(db,session,request)
    assert staged.outcome==booking.BookingOutcome.DRAFT, staged.message
    assert not (await db.execute(select(Appointment).where(Appointment.status==AppointmentStatus.SCHEDULED))).first()
    arm_verified_proposal(session)
    booked=await booking.confirm_booking(db,session)
    assert booked.outcome==booking.BookingOutcome.BOOKED, booked.message
    rows=list((await db.execute(select(Appointment).where(Appointment.status==AppointmentStatus.SCHEDULED))).scalars())
    assert len(rows)==1
    row=rows[0]
    assert row.external_booking_id=='visit-1'
    assert row.end_time-row.start_time==timedelta(minutes=150)
    # SQLite drops timezone metadata; production PostgreSQL uses timestamptz.
    saved=row.start_time.replace(tzinfo=timezone.utc) if row.start_time.tzinfo is None else row.start_time
    assert saved==utc_instant(ctx.start)
    assert 'European facial' in row.description and 'Deep tissue massage' in row.description
    assert len([path for _,path,_ in state['calls'] if path=='/v2/bookings'])==1


def test_compound_service_keeps_selected_duration_on_confirmation():
    from app.services.booking_state import BookingDraft
    draft=BookingDraft(service_description='Face and Neck',selected_slot={'duration_minutes':60})
    assert booking._draft_intent(draft).service_description=='Face and Neck (60 min)'


def test_multi_service_recheck_does_not_apply_total_duration_to_last_service():
    from app.services.booking_state import BookingDraft
    draft=BookingDraft(service_description='European facial + Deep tissue massage',selected_slot={'duration_minutes':150,'visit_segments':[{},{}]})
    assert booking._draft_intent(draft).service_description=='European facial + Deep tissue massage'


@pytest.mark.parametrize('timestamp',[None,'2027-10-08T14:00:00'])
async def test_missing_or_naive_provider_time_is_lookup_failure(visit,timestamp):
    router,ctx,_=visit
    original=router.delegate._request
    async def invalid_time(method,path,**kwargs):
        result=await original(method,path,**kwargs)
        if path.endswith('/availability/search'):
            result['availabilities'][0]['start_at']=timestamp
        return result
    router.delegate._request=invalid_time
    with pytest.raises(BookingProviderError) as caught: await router.check_availability(ctx)
    assert caught.value.code=='INVALID_RESPONSE'
