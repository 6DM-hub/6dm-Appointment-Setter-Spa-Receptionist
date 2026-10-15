"""Independent R5 candidate tests. UNEXECUTED: environment lacks pytest/dependencies.

Run through the supplied trusted verifier with --extra-tests. Only harness
catalog fixtures are extended; application code and frozen tests are unchanged.
These tests cover transcript/state boundaries, not audio/model performance.
"""
import json
import pytest
from tests.test_brick1_booking_change_guard import (
    BUSINESSES, Phone, LegacyCall, make_world, booked_call, _legacy_booked,
    _intent, T2, T3, local, reconnect,
)
from tests.test_codex_brick1_r2_independent import offered
from tests.test_brick1_repair2 import cancellation_transport
from app.services.booking_state import note_backend_prompt
from app.services.call_state import CallSession

INDUSTRIES = {
    'spa': ('Swedish Massage', 'Signature Facial'),
    'cleaning': ('House Cleaning', 'Window Cleaning'),
    'roofing': ('Roof Estimate', 'Roof Inspection'),
    'construction': ('Site Survey', 'Remodel Consultation'),
    'hvac': ('AC Tune-Up', 'Furnace Inspection'),
    'dental': ('Teeth Cleaning', 'Dental Exam'),
    'plumbing': ('Leak Inspection', 'Drain Cleaning'),
}

def world_for(monkeypatch, industry):
    # Keep provider IDs stable so service-name neutrality is the variable.
    names = INDUSTRIES[industry]
    menu = [dict(BUSINESSES['spa'][i], name=name) for i, name in enumerate(names)]
    monkeypatch.setitem(BUSINESSES, industry, menu)
    return make_world(monkeypatch, industry)

@pytest.mark.parametrize('industry', list(INDUSTRIES))
@pytest.mark.parametrize('path', ['realtime', 'legacy'])
@pytest.mark.parametrize('question,answer', [
    ('Would you like directions before deciding whether to book?', 'Yes.'),
    ('Would you like me to book it; and do you need the address', 'Yes.'),
    ('¿Quiere la dirección antes de decidir?', 'Sí.'),
    ('Voulez-vous notre adresse avant de décider ?', 'Oui.'),
    ('Möchten Sie zuerst unsere Adresse?', 'Ja.'),
])
async def test_latest_unrelated_or_compound_question_never_authorizes(monkeypatch, industry, path, question, answer):
    w = world_for(monkeypatch, industry)
    p = await offered(w, monkeypatch, path)
    p.session.add_turn('assistant', question)
    await p.says(answer)
    assert not w.square.creates, ('UNAUTHORIZED_CREATE', industry, path, question, w.square.creates)

@pytest.mark.parametrize('industry', list(INDUSTRIES))
@pytest.mark.parametrize('path', ['realtime', 'legacy'])
@pytest.mark.parametrize('withdrawal', ['Never mind, keep it.', 'No, déjalo como está.'])
async def test_withdrawn_move_survives_serialization_and_delayed_tools(monkeypatch, industry, path, withdrawal):
    w = world_for(monkeypatch, industry)
    p = await booked_call(w) if path == 'realtime' else await _legacy_booked(w, monkeypatch)
    if path == 'realtime':
        await p.says('Move my appointment to Thursday at 3 PM instead.')
        await p.propose(operation='reschedule', requested_start_iso=local(T3), service_description=w.first)
    else:
        await p.says('Move my appointment to Thursday at 3 PM instead.',
                     intent=_intent('reschedule', T3, w.first), reply='Shall I move it to Thursday at 3 PM?')
    await p.says(withdrawal)
    # Redis JSON round trip, followed by stale confirmation and tool delivery.
    p.session = CallSession.from_dict(json.loads(json.dumps(p.session.to_dict())))
    if path == 'realtime':
        p = reconnect(p)
        await p.tool('confirm_appointment')
        await p.says('Yes.')
        await p.tool('confirm_appointment')
    else:
        await p.says('Yes.', intent=_intent('reschedule', T3, w.first))
    assert len(w.square.creates) == 1 and not w.square.updates, (industry, path, w.square.calls)

@pytest.mark.parametrize('path', ['realtime', 'legacy'])
@pytest.mark.parametrize('question,answer', [
    ('Solo para confirmar, ¿quiere que cancele su cita del jueves a las 2 PM?', 'Sí.'),
    ('Confirmez-vous l’annulation de votre rendez-vous jeudi à 14 heures ?', 'Oui.'),
])
async def test_translated_cancellation_has_positive_control(monkeypatch, path, question, answer):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w) if path == 'realtime' else await _legacy_booked(w, monkeypatch)
    if path == 'realtime':
        await p.says('Please cancel my appointment.')
        await p.tool('cancel_appointment')
    else:
        await p.says('Please cancel my appointment.', intent=_intent('cancel', T2, w.first))
    note_backend_prompt(p.session, question)
    p.session.add_turn('assistant', question)
    await p.says(answer)
    assert not w.square.active(), ('TRANSLATED_CANCEL_NOT_COMPLETED', path, question)

@pytest.mark.parametrize('path', ['realtime', 'legacy'])
async def test_translated_cancel_question_replayed_after_withdrawal_has_no_authority(monkeypatch, path):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w) if path == 'realtime' else await _legacy_booked(w, monkeypatch)
    if path == 'realtime':
        await p.says('Please cancel my appointment.')
        await p.tool('cancel_appointment')
    else:
        await p.says('Please cancel my appointment.', intent=_intent('cancel', T2, w.first))
    question = 'Solo para confirmar, ¿quiere que cancele su cita del jueves a las 2 PM?'
    note_backend_prompt(p.session, question)
    p.session.add_turn('assistant', question)
    await p.says('Never mind, keep it.')
    p.session.add_turn('assistant', question)  # replay with no new backend occurrence
    await p.says('Sí.')
    if path == 'realtime':
        await p.tool('cancel_appointment')
    assert w.square.active() and not any(r.endswith('/cancel') for _, r, _ in w.square.calls)
