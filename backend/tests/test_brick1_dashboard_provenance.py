"""Round5 5c/5d isolated audit attempts; no real providers."""
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from app.models import Appointment, AppointmentStatus, Contact
from app.services import appointment_booking_service as booking
from app.services import xai_realtime as realtime
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent
from app.services.scheduling_time import elapsed_end, utc_instant
from tests.test_multi_service_visit import visit
from tests.test_cara_native_double_booking import rubyz_db


@pytest.mark.parametrize('purpose', ['lookup', 'history'])
async def test_dashboard_only_row_is_not_presented_as_provider_verified(rubyz_db, monkeypatch, purpose):
    db,spa=rubyz_db
    contact=Contact(id=uuid.uuid4(),tenant_id=spa.id,phone_number='+15550009102')
    db.add(contact)
    await db.flush()
    start=datetime.now(timezone.utc)+timedelta(days=2 if purpose=='lookup' else -2)
    row=Appointment(id=uuid.uuid4(),tenant_id=spa.id,contact_id=contact.id,title='Dashboard-only visit',start_time=start,end_time=start+timedelta(hours=1),status=AppointmentStatus.SCHEDULED)
    db.add(row)
    await db.commit()
    caller=CallSession('dashboard-lookup','inbound',contact.phone_number,spa.twilio_phone_number,tenant_id=str(spa.id),timezone=spa.timezone)
    @asynccontextmanager
    async def db_context():
        yield db
    monkeypatch.setattr(realtime,'AsyncSessionLocal',db_context)
    voice=realtime.XAIVoiceSession.__new__(realtime.XAIVoiceSession)
    voice.session=caller
    voice._persist_session=AsyncMock()
    result=json.loads(await voice._run_lookup_appointments(json.dumps({'purpose':purpose})))
    print('DASHBOARD_LOOKUP',json.dumps(result))
    assert len(result['appointments']) == 1
    item=result['appointments'][0]
    assert item.get('source') == 'dashboard_only', 'DASHBOARD ROW HAS NO PROVIDER-UNVERIFIED MARKER'
    assert item.get('approval_status') != 'accepted', 'DASHBOARD ROW FALSELY PROVIDER ACCEPTED'


@pytest.mark.parametrize("path", ["legacy"])
async def test_local_only_cancel_never_claims_external_cancellation(monkeypatch, path):
    from tests.test_brick1_booking_change_guard import make_world, booked_call, _legacy_booked, _intent, T2
    world = make_world(monkeypatch, "spa")
    phone = await booked_call(world) if path == "realtime" else await _legacy_booked(world, monkeypatch)
    row = world.store.live[0]
    row.external_booking_id = None
    row.booking_intent_key = None
    row.booking_provider = None
    if path == "realtime":
        from tests.test_brick1_booking_change_guard import Phone
        phone = Phone(world)
        await phone.says("Please cancel my appointment.")
        await phone.tool("cancel_appointment", appointment_id=str(row.id))
        said = " ".join(await phone.says("Yes."))
    else:
        await phone.says("Please cancel my appointment.", intent=_intent("cancel", T2, world.first))
        said = await phone.says("Yes.", reply="Your Square appointment is cancelled.")
    assert row.status == AppointmentStatus.CANCELLED
    assert len(world.square.active()) == 1, "LOCAL CANCEL TOUCHED PROVIDER"
    assert "CARA" in said and "external" in said.lower(), said


def test_realtime_local_cancel_followup_discloses_local_scope():
    voice = realtime.XAIVoiceSession.__new__(realtime.XAIVoiceSession)
    speech = voice._authoritative_tool_followup("cancel_appointment", json.dumps({
        "status": "cancelled", "appointment_id": "local-row", "source": "dashboard_only",
        "service": "Visit"}))
    assert "CARA" in speech and "external" in speech.lower(), speech


@pytest.mark.parametrize("provider,external,intent,status,source,approval", [
    (None,None,None,"scheduled","dashboard_only","not_verified"),
    ("cara_native","native-id","intent","scheduled","cara_booking_record","local_booking"),
    ("square","square-id","intent","awaiting_provider_approval","provider_linked_record","awaiting_provider_approval"),
    ("square",None,"intent","scheduled","local_booking_record","not_verified"),
])
def test_provenance_keeps_native_pending_and_unverified_distinct(provider,external,intent,status,source,approval):
    from types import SimpleNamespace
    from app.services.appointment_presentation import appointment_provenance
    row=SimpleNamespace(booking_provider=provider,external_booking_id=external,booking_intent_key=intent,status=status)
    result=appointment_provenance(row)
    assert result['source']==source and result['approval_status']==approval
