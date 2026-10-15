"""Independent R3 challenges. Provider transport simulated; application code untouched."""
import copy
import pytest
from fastapi import HTTPException
from app.services import booking_change_guard as guard
from app.services.booking_adapters.base import BookingProviderError
from tests.test_brick1_booking_change_guard import Phone, LegacyCall, T2, T3, local, _intent, make_world, booked_call, _legacy_booked, reconnect
from tests.test_brick1_repair2 import cancellation_transport
from tests.test_codex_brick1_r2_independent import offered
from tests.test_cara_native_double_booking import rubyz_db
from tests.test_codex_brick1_tenant_boundaries import setup_tenants, session_for, book

@pytest.mark.parametrize('path',['realtime','legacy'])
@pytest.mark.parametrize('shape',['no_question_mark','combined_question'])
async def test_unrelated_question_cannot_authorize_booking(monkeypatch,path,shape):
 w=make_world(monkeypatch)
 p=await offered(w,monkeypatch,path)
 original=(p.session.entities.get('open_booking_question') or {}).get('text') or 'Thursday at 2 PM is available. Would you like me to book that?'
 question='Would you like our address' if shape=='no_question_mark' else original+' Would you like our address before deciding?'
 p.session.add_turn('assistant',question)
 await p.says('Yes.')
 assert not w.square.creates, ('UNAUTHORIZED_CREATE',path,shape,question,w.square.creates)

@pytest.mark.parametrize('path',['realtime','legacy'])
@pytest.mark.parametrize('shape',['no_question_mark','combined_question'])
async def test_unrelated_question_cannot_authorize_cancellation(monkeypatch,path,shape):
 w=make_world(monkeypatch);cancellation_transport(monkeypatch,w)
 p=await booked_call(w) if path=='realtime' else await _legacy_booked(w,monkeypatch)
 if path=='realtime':
  await p.says('Please cancel my appointment.');await p.tool('cancel_appointment')
 else:await p.says('Please cancel my appointment.',intent=_intent('cancel',T2,w.first))
 pending=p.session.entities.get(guard.PENDING_CANCELLATION)
 assert pending, 'Fixture did not stage a cancellation'
 question='Would you like our address' if shape=='no_question_mark' else pending['question']+' Would you like our address before deciding?'
 p.session.add_turn('assistant',question)
 await p.says('Yes.')
 assert w.square.active(), ('UNAUTHORIZED_CANCEL',path,shape,question,w.square.calls)
 assert not any(path.endswith('/cancel') for _,path,_ in w.square.calls)

@pytest.mark.parametrize('path',['realtime','legacy'])
@pytest.mark.parametrize('words',['Never mind.','Keep it.','No, do not cancel it.'])
async def test_withdrawn_cancellation_plus_late_duplicate_tool_keeps_original(monkeypatch,path,words):
 w=make_world(monkeypatch);cancellation_transport(monkeypatch,w)
 p=await booked_call(w) if path=='realtime' else await _legacy_booked(w,monkeypatch)
 if path=='realtime':
  await p.says('Please cancel my appointment.');await p.tool('cancel_appointment');await p.says(words)
  await p.tool('cancel_appointment');await p.says('Yes.');await p.tool('cancel_appointment')
 else:
  await p.says('Please cancel my appointment.',intent=_intent('cancel',T2,w.first));await p.says(words,intent=_intent('cancel',T2,w.first));await p.says('Yes.',intent=_intent('cancel',T2,w.first))
 assert w.square.active() and not any(path.endswith('/cancel') for _,path,_ in w.square.calls)

@pytest.mark.parametrize('operation',['move','cancel'])
async def test_dashboard_unknown_outcome_blocks_later_writes(rubyz_db,monkeypatch,operation):
 from app.api.v1.appointments import cancel_appointment,update_appointment
 from app.schemas import AppointmentUpdate
 from datetime import timedelta
 db,a,b,squares=await setup_tenants(rubyz_db,monkeypatch)
 session=session_for(a,'r3-dashboard-unknown')
 row=await book(db,a,session);row_id=row.id;scope=session.scope
 square=squares['fake-cleaning-token'];original=square.__call__
 async def lose_response(method,route,*,json=None):
  if method=='POST' and route.endswith('/cancel'):
   square.calls.append((method,route,copy.deepcopy(json)))
   held=square.bookings[route.split('/')[-2]];held['status']='CANCELLED_BY_CUSTOMER'
   raise BookingProviderError('lost cancellation response',code='BOOKING_OUTCOME_UNKNOWN')
  result=await original(method,route,json=json)
  if method=='PUT':raise BookingProviderError('lost move response',code='BOOKING_OUTCOME_UNKNOWN')
  return result
 squares['fake-cleaning-token']=lose_response
 with pytest.raises(HTTPException) as first:
  if operation=='move':await update_appointment(row_id,AppointmentUpdate(start_time=T3,end_time=T3+timedelta(hours=1)),scope=scope,db=db)
  else:await cancel_appointment(row_id,scope=scope,db=db)
 assert first.value.status_code==502
 writes_before=len([x for x in square.calls if x[0]=='PUT' or x[1].endswith('/cancel')])
 # The next request has a fresh database transaction, just like a new HTTP request.
 await db.rollback()
 try:await cancel_appointment(row_id,scope=scope,db=db)
 except HTTPException:pass
 writes_after=len([x for x in square.calls if x[0]=='PUT' or x[1].endswith('/cancel')])
 assert writes_after==writes_before, ('UNRESOLVED_DASHBOARD_CHANGE_NOT_BLOCKED',operation,writes_before,writes_after)

@pytest.mark.parametrize('path',['realtime','legacy'])
async def test_thin_cancel_reply_readback_must_recheck_booking_identity(monkeypatch,path):
 w=make_world(monkeypatch)
 p=await booked_call(w) if path=='realtime' else await _legacy_booked(w,monkeypatch)
 original=w.square.__call__;pending_readback=False
 async def wrong_readback(method,route,*,json=None):
  nonlocal pending_readback
  if method=='POST' and route.endswith('/cancel'):
   w.square.calls.append((method,route,copy.deepcopy(json)));pending_readback=True
   return {'booking':{'id':route.split('/')[-2]}}  # Thin reply, no proof of success.
  if method=='GET' and pending_readback:
   return {'booking':{'id':'unrelated-booking','status':'CANCELLED_BY_CUSTOMER','version':3}}
  return await original(method,route,json=json)
 monkeypatch.setattr(w.adapter.delegate,'_request',wrong_readback)
 if path=='realtime':
  await p.says('Please cancel my appointment.');await p.tool('cancel_appointment');speech=await p.says('Yes.')
 else:
  await p.says('Please cancel my appointment.',intent=_intent('cancel',T2,w.first));speech=[await p.says('Yes.')]
 assert any(route.endswith('/cancel') for _,route,_ in w.square.calls), 'Fixture must reach the provider cancellation'
 assert p.session.entities.get('booking_reconciliation_required'), ('WRONG_BOOKING_READBACK_REPORTED_CANCELLED',path,speech,bool(w.store.live),w.square.active())
 assert w.store.live, 'The local row must remain active until this booking is verified'

async def test_interrupted_move_must_block_mutations_from_next_call(rubyz_db,monkeypatch):
 import asyncio
 from app.services import appointment_booking_service as booking
 from app.services.booking_state import arm_verified_proposal
 from app.services.grok_service import AppointmentIntent
 db,a,b,squares=await setup_tenants(rubyz_db,monkeypatch)
 first=session_for(a,'r3-interrupted');row=await book(db,a,first);square=squares['fake-cleaning-token'];square.open_all(T3)
 async def interrupted(method,route,*,json=None):
  result=await square(method,route,json=json)
  if method=='PUT':raise asyncio.CancelledError('simulated worker task interrupted after provider applied move')
  return result
 squares['fake-cleaning-token']=interrupted
 first.add_turn('user','Move my appointment to Thursday at 3 PM instead.')
 staged=await booking.stage_booking(db,first,AppointmentIntent(intent='reschedule',confidence=1,service_description=a.services[0]['name'],requested_start_iso=local(T3)))
 assert staged.outcome==booking.BookingOutcome.DRAFT
 arm_verified_proposal(first)
 with pytest.raises(asyncio.CancelledError):await booking.confirm_booking(db,first)
 await db.rollback();await db.refresh(a)
 before=len([c for c in square.calls if c[0]=='PUT' or c[1].endswith('/cancel')])
 # Restore a transport that can accept cancellation. No changes to the application.
 async def new_transport(method,route,*,json=None):
  if method=='POST' and route.endswith('/cancel'):
   square.calls.append((method,route,copy.deepcopy(json)));held=square.bookings[route.split('/')[-2]];held['status']='CANCELLED_BY_CUSTOMER';return {'booking':copy.deepcopy(held)}
  return await square(method,route,json=json)
 squares['fake-cleaning-token']=new_transport
 later=session_for(a,'r3-new-call');later.add_turn('user','Please cancel my appointment.')
 asked=await booking.cancel_booking(db,later)
 if guard.PENDING_CANCELLATION in later.entities:
  later.add_turn('user','Yes.');await booking.cancel_booking(db,later)
 after=len([c for c in square.calls if c[0]=='PUT' or c[1].endswith('/cancel')])
 assert after==before, ('INTERRUPTED_CHANGE_NOT_DURABLY_BLOCKED',asked.outcome,before,after)

@pytest.mark.parametrize('path',['realtime','legacy'])
@pytest.mark.parametrize('operation',['create','cancel'])
async def test_clear_unrelated_question_is_a_negative_control(monkeypatch,path,operation):
 w=make_world(monkeypatch);cancellation_transport(monkeypatch,w)
 if operation=='create':p=await offered(w,monkeypatch,path)
 else:
  p=await booked_call(w) if path=='realtime' else await _legacy_booked(w,monkeypatch)
  if path=='realtime':await p.says('Please cancel my appointment.');await p.tool('cancel_appointment')
  else:await p.says('Please cancel my appointment.',intent=_intent('cancel',T2,w.first))
 p.session.add_turn('assistant','Would you like our address?')
 await p.says('Yes.')
 if operation=='create':assert not w.square.creates
 else:assert w.square.active() and not any(route.endswith('/cancel') for _,route,_ in w.square.calls)
