"""Independent R4 checks. A passing assertion means required safe behavior.
Application source and all supplied tests remain unchanged. Provider HTTP simulated.
"""
import pytest
from app.services.booking_state import note_backend_prompt, CARD_LINK_PERMISSION_QUESTION
from tests.test_brick1_booking_change_guard import Phone, LegacyCall, T2, T3, local, _intent, make_world, booked_call, _legacy_booked
from tests.test_brick1_repair2 import cancellation_transport

@pytest.mark.parametrize('business',['spa','hvac','dental'])
@pytest.mark.parametrize('operation',['create','move'])
@pytest.mark.parametrize('cached',[True,False])
@pytest.mark.parametrize('path',['realtime','legacy'])
@pytest.mark.parametrize('line',[CARD_LINK_PERMISSION_QUESTION,'May I ask a service provider to call you back?'])
async def test_old_backend_question_replayed_by_model_cannot_authorize_booking(monkeypatch,path,line,cached,operation,business):
 w=make_world(monkeypatch,business)
 if operation=='move':p=await booked_call(w) if path=='realtime' else await _legacy_booked(w,monkeypatch)
 else:p=Phone(w) if path=='realtime' else LegacyCall(w,monkeypatch)
 # A real backend-authored line from an EARLIER phase. Cache outlives its handler.
 if cached:note_backend_prompt(p.session,line)
 p.session.add_turn('assistant',line)
 p.session.add_turn('user','No, thank you.')
 words=f'A {w.first} Thursday at 2 PM. My name is Ada Lovelace.' if operation=='create' else 'Move my appointment to Thursday at 3 PM instead.'
 target=T2 if operation=='create' else T3
 mode='schedule' if operation=='create' else 'reschedule'
 if path=='realtime':
  await p.says(words)
  out=await p.propose(operation=mode,requested_start_iso=local(target),service_description=w.first,caller_name='Ada Lovelace')
  assert out['status']=='draft'
 else:await p.says(words,intent=_intent(mode,target,w.first),reply='Thursday at 2 PM is available. Would you like me to book that?' if operation=='create' else 'Thursday at 3 PM is available. Would you like me to move it?')
 # The model repeats that unrelated question after the actual booking question.
 p.session.add_turn('assistant',line)
 await p.says('Yes.')
 if operation=='create':assert not w.square.creates, ('YES_TO_REPLAYED_OTHER_QUESTION_CREATED',path,line,w.square.creates)
 else:assert not w.square.updates, ('YES_TO_REPLAYED_OTHER_QUESTION_MOVED',path,line,w.square.updates)

@pytest.mark.parametrize('path',['realtime','legacy'])
async def test_translated_cancel_question_can_be_confirmed_by_spanish_yes(monkeypatch,path):
 w=make_world(monkeypatch);cancellation_transport(monkeypatch,w)
 p=await booked_call(w) if path=='realtime' else await _legacy_booked(w,monkeypatch)
 if path=='realtime':await p.says('Please cancel my appointment.');await p.tool('cancel_appointment')
 else:await p.says('Please cancel my appointment.',intent=_intent('cancel',T2,w.first))
 translated='Solo para confirmar, ¿quiere que cancele su masaje del jueves a las 2 PM?'
 note_backend_prompt(p.session,translated);p.session.add_turn('assistant',translated)
 await p.says('Sí.')
 assert not w.square.active(), ('TRANSLATED_CANCELLATION_NOT_CONFIRMED',path)

from tests.test_cara_native_double_booking import rubyz_db
from tests.test_codex_brick1_tenant_boundaries import setup_tenants,session_for,book
from app.services import appointment_booking_service as booking
from fastapi import HTTPException
from datetime import timedelta
from types import SimpleNamespace

@pytest.mark.parametrize('operation',['move','cancel'])
@pytest.mark.parametrize('configured',[True,False])
async def test_google_calendar_failure_or_disconnection_cannot_be_reported_as_dashboard_success(rubyz_db,monkeypatch,operation,configured):
 from app.api.v1.appointments import cancel_appointment,update_appointment
 from app.schemas import AppointmentUpdate
 from app.models import BookingProvider,AppointmentStatus
 from app.services.booking_adapters.google_calendar import GoogleCalendarAdapter
 from app.services.booking_adapters import google_calendar as gcal
 from app.services.google_calendar import GoogleCalendarError
 from tests.test_brick1_booking_change_guard import T3
 db,a,b,squares=await setup_tenants(rubyz_db,monkeypatch)
 call=session_for(a,'r4-google');row=await book(db,a,call)
 row.booking_provider=BookingProvider.GOOGLE_CALENDAR.value
 row.external_booking_id='google-event-existing'
 await db.commit()
 row_id,scope=row.id,call.scope;prior_start=row.start_time;prior_status=row.status
 # Stable routing boundary chooses the REAL configured Google adapter.
 adapter=GoogleCalendarAdapter(calendar_id='test-calendar',connection=SimpleNamespace() if configured else None)
 monkeypatch.setattr(booking,'get_booking_adapter',lambda **kw:adapter)
 calls=[]
 async def refused(*args,**kwargs):
  calls.append(operation)
  raise GoogleCalendarError('provider_error','Simulated provider refused the write')
 monkeypatch.setattr(gcal,'delete_event',refused);monkeypatch.setattr(gcal,'update_event',refused)
 error=None
 try:
  if operation=='cancel':await cancel_appointment(row_id,scope=scope,db=db)
  else:await update_appointment(row_id,AppointmentUpdate(start_time=T3,end_time=T3+timedelta(hours=1)),scope=scope,db=db)
 except HTTPException as e:error=e.status_code
 await db.refresh(row)
 assert calls==([operation] if configured else []), ('GOOGLE_FIXTURE_DID_NOT_REACH_PROVIDER',error,calls)
 assert (row.status,row.start_time)==(prior_status,prior_start), ('FALSE_GOOGLE_SUCCESS',operation,configured,error,calls,str(row.status),str(row.start_time))
 assert error is not None, 'A failed/disconnected calendar must not return success'

async def test_external_change_without_tenant_cannot_be_sent_unfenced():
 import uuid
 from app.services.call_state import CallSession
 call=CallSession('r4-owner-scope','outbound','+15550000001','+15550000002',user_id=str(uuid.uuid4()))
 row=SimpleNamespace(id=uuid.uuid4(),external_booking_id='external-existing',title='Roofing estimate',start_time=None)
 refusal=await booking._begin_provider_change(None,call,'cancel',row)
 assert refusal is not None, ('EXTERNAL_WRITE_ALLOWED_WITHOUT_DURABLE_FENCE',call.entities)
