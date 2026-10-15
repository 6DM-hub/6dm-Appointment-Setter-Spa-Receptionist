"""Round-two independent safety requirements; only provider HTTP is simulated."""
import copy
import pytest
from fastapi import HTTPException
from app.models import AppointmentStatus
from app.services.booking_adapters.base import BookingProviderError
from tests.test_brick1_booking_change_guard import (
    Phone, LegacyCall, T2, T3, Z2, make_world, booked_call, _legacy_booked, _intent, local,
)
from tests.test_brick1_repair2 import cancellation_transport
from tests.test_cara_native_double_booking import rubyz_db
from tests.test_codex_brick1_tenant_boundaries import setup_tenants, session_for, book


async def offered(w, monkeypatch, path):
    text = f'A {w.first} Thursday at 2 PM. My name is Ada Lovelace.'
    if path == 'realtime':
        p = Phone(w)
        await p.says(text)
        out = await p.propose(operation='schedule', requested_start_iso=local(T2),
                              service_description=w.first, caller_name='Ada Lovelace')
        assert out['status'] == 'draft'
    else:
        p = LegacyCall(w, monkeypatch)
        await p.says(text, intent=_intent('schedule', T2, w.first),
                     reply='Thursday at 2 PM is available. Would you like me to book that?')
    return p


@pytest.mark.parametrize('path', ['realtime', 'legacy'])
@pytest.mark.parametrize('question', [
    'Would you like the address for your appointment?',
    'Would you like me to explain our cancellation policy before you book?',
    'Should I tell you our address?',
    'Would you like me to book that? First, do you need directions?',
    'Would you like our address? I can give it to you.',
])
async def test_yes_to_unrelated_question_never_creates(monkeypatch, path, question):
    w = make_world(monkeypatch)
    p = await offered(w, monkeypatch, path)
    p.session.add_turn('assistant', question)
    await p.says('Yes.')
    assert not w.square.creates, ('UNAUTHORIZED CREATE', question, w.square.creates)


@pytest.mark.parametrize('path', ['realtime', 'legacy'])
@pytest.mark.parametrize('words', [
    'What is your cancellation policy?',
    'Would there be a fee if I cancel my appointment?',
    'I cancelled my other appointment, keep this one.',
    'I am not sure whether I should cancel.',
    "Do not under any circumstances cancel my appointment.",
    "Don't under any circumstances cancel my appointment.",
])
async def test_discussing_cancellation_does_not_authorize_it(monkeypatch, path, words):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    if path == 'realtime':
        p = await booked_call(w)
        await p.says(words)
        await p.tool('cancel_appointment')
    else:
        p = await _legacy_booked(w, monkeypatch)
        await p.says(words, intent=_intent('cancel', T2, w.first))
    assert w.square.active(), ('UNAUTHORIZED CANCEL', words, w.square.calls)
    assert not any(path.endswith('/cancel') for _, path, _ in w.square.calls)


@pytest.mark.parametrize('path', ['realtime', 'legacy'])
async def test_cancel_confirmation_yes_must_answer_cancel_question(monkeypatch, path):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    if path == 'realtime':
        p = await booked_call(w)
        await p.says('Please cancel my appointment.')
        p.session.add_turn('assistant', 'Would you like our address instead?')
        await p.says('Yes.')
        await p.tool('cancel_appointment')
    else:
        p = await _legacy_booked(w, monkeypatch)
        await p.says('Please cancel my appointment.', reply='Would you like our address instead?')
        await p.says('Yes.', intent=_intent('cancel', T2, w.first))
    assert w.square.active(), ('UNRELATED YES CANCELLED', w.square.calls)


@pytest.mark.parametrize('path', ['realtime', 'legacy'])
@pytest.mark.parametrize('code', ['FORBIDDEN', 'BOOKING_OUTCOME_UNKNOWN'])
async def test_provider_cancel_error_never_reports_success(monkeypatch, path, code):
    w = make_world(monkeypatch)
    p = await booked_call(w) if path == 'realtime' else await _legacy_booked(w, monkeypatch)
    original = w.square.__call__
    async def fail(method, route, *, json=None):
        if method == 'POST' and route.endswith('/cancel'):
            raise BookingProviderError('simulated refusal or lost response', code=code)
        return await original(method, route, json=json)
    monkeypatch.setattr(w.adapter.delegate, '_request', fail)
    if path == 'realtime':
        await p.says('Please cancel my appointment.')
        result = await p.tool('cancel_appointment')
        assert result.get('status') != 'cancelled', (result, w.square.active())
    else:
        speech = await p.says('Please cancel my appointment.', intent=_intent('cancel', T2, w.first))
        assert 'has been cancelled' not in speech.lower(), speech
    assert len(w.square.active()) == 1
    assert w.store.live, 'provider refusal must preserve local active row'


@pytest.mark.parametrize('reply', [{'id': 'sq_1'}, {'id':'wrong-booking', 'status':'CANCELLED_BY_CUSTOMER'}])
async def test_cancel_reply_requires_exact_identity_and_cancelled_status(monkeypatch, reply):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    original = w.square.__call__
    async def thin(method, route, *, json=None):
        if method == 'POST' and route.endswith('/cancel'):
            return {'booking': copy.deepcopy(reply)}
        return await original(method, route, json=json)
    monkeypatch.setattr(w.adapter.delegate, '_request', thin)
    await p.says('Please cancel my appointment.')
    out = await p.tool('cancel_appointment')
    assert out.get('status') != 'cancelled', (reply, out, w.square.active())


@pytest.mark.parametrize('method', ['delete', 'patch'])
async def test_dashboard_provider_refusal_preserves_local_row(rubyz_db, monkeypatch, method):
    from app.api.v1.appointments import cancel_appointment, update_appointment
    from app.schemas import AppointmentUpdate
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    session = session_for(a, 'r2-dashboard')
    row = await book(db, a, session)
    original = squares['fake-cleaning-token']
    async def fail(verb, route, *, json=None):
        if verb == 'POST' and route.endswith('/cancel'):
            raise BookingProviderError('simulated forbidden', code='FORBIDDEN')
        return await original(verb, route, json=json)
    squares['fake-cleaning-token'] = fail
    with pytest.raises(HTTPException) as error:
        if method == 'delete':
            await cancel_appointment(row.id, scope=session.scope, db=db)
        else:
            await update_appointment(row.id, AppointmentUpdate(status=AppointmentStatus.CANCELLED),
                                     scope=session.scope, db=db)
    assert error.value.status_code == 409
    await db.refresh(row)
    assert row.status != AppointmentStatus.CANCELLED


async def test_foreign_customer_id_in_contaminated_offer_cannot_cancel(rubyz_db, monkeypatch):
    from app.services import appointment_booking_service as booking
    from app.services.booking_state import get_draft, save_draft
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    victim = await book(db, a, session_for(a, 'r2-victim'))
    attacker = session_for(a, 'r2-other', phone='+15559999999')
    attacker.add_turn('user', 'Please cancel my appointment.')
    draft = get_draft(attacker)
    draft.offered_appointment_ids = [str(victim.id)]
    save_draft(attacker, draft)
    result = await booking.cancel_booking(db, attacker, appointment_id=str(victim.id))
    assert result.outcome != booking.BookingOutcome.CANCELLED
    assert squares['fake-cleaning-token'].active()


@pytest.mark.parametrize('field', ['customer', 'location', 'duration', 'status'])
async def test_move_response_must_match_all_authorized_fields(monkeypatch, field):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    original = w.square.__call__
    async def corrupt(method, route, *, json=None):
        result = await original(method, route, json=json)
        if method == 'PUT':
            booking = w.square.bookings[route.rsplit('/', 1)[-1]]
            if field == 'customer':
                booking['customer_id'] = 'someone-else'
            elif field == 'location':
                booking['location_id'] = 'other-business-location'
            elif field == 'duration':
                booking['appointment_segments'][0]['duration_minutes'] = 15
            else:
                booking['status'] = 'CANCELLED_BY_SELLER'
            return {'booking': copy.deepcopy(booking)}
        return result
    monkeypatch.setattr(w.adapter.delegate, '_request', corrupt)
    await p.says('Move my appointment to Thursday at 3 PM instead.')
    assert (await p.propose(operation='reschedule', requested_start_iso=local(T3),
                            service_description=w.first))['status'] == 'draft'
    await p.says('Yes, please.')
    assert p.session.entities.get('booking_reconciliation_required'), (
        'MISMATCH REPORTED AS VERIFIED MOVE', field, p.session.booking_status, p.heard)


@pytest.mark.parametrize('question', ['Would you like our address?', 'Should I tell you our address?'])
async def test_unrelated_yes_after_reconnect_cannot_book(monkeypatch, question):
    from tests.test_brick1_booking_change_guard import reconnect
    w = make_world(monkeypatch)
    p = await offered(w, monkeypatch, 'realtime')
    p.session.add_turn('assistant', question)
    p = reconnect(p)
    await p.says('Yes.')
    await p.tool('confirm_appointment')
    assert not w.square.creates, ('RECONNECT UNAUTHORIZED CREATE', question, w.square.creates)


@pytest.mark.parametrize('business', ['spa', 'hvac', 'dental'])
async def test_policy_question_cannot_cancel_in_any_industry(monkeypatch, business):
    w = make_world(monkeypatch, business)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says('What is your cancellation policy?')
    await p.tool('cancel_appointment')
    assert w.square.active(), ('UNAUTHORIZED INDUSTRY CANCEL', business)


async def test_unknown_move_blocks_mutations_from_a_later_call(rubyz_db, monkeypatch):
    from app.services import appointment_booking_service as booking
    from app.services.booking_state import arm_verified_proposal
    from app.services.grok_service import AppointmentIntent
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    original_call = session_for(a, 'r2-lost-move')
    row = await book(db, a, original_call)
    square = squares['fake-cleaning-token']
    square.open_all(T3)
    async def lose_response(method, route, *, json=None):
        result = await square(method, route, json=json)
        if method == 'PUT':
            raise BookingProviderError('simulated response lost after applying move',
                                       code='BOOKING_OUTCOME_UNKNOWN', retryable=False)
        return result
    squares['fake-cleaning-token'] = lose_response
    original_call.add_turn('user', 'Move my appointment to Thursday at 3 PM instead.')
    staged = await booking.stage_booking(db, original_call, AppointmentIntent(
        intent='reschedule', confidence=1, service_description=a.services[0]['name'],
        requested_start_iso=local(T3)))
    assert staged.outcome == booking.BookingOutcome.DRAFT, staged.message
    arm_verified_proposal(original_call)
    result = await booking.confirm_booking(db, original_call)
    assert result.outcome == booking.BookingOutcome.ERROR
    assert original_call.entities.get('booking_reconciliation_required')
    writes_before = len([c for c in square.calls if c[0] == 'PUT' or c[1].endswith('/cancel')])
    await db.refresh(a)  # the real rollback expires ORM identity objects
    new_call = session_for(a, 'r2-after-disconnect')
    new_call.add_turn('user', 'Please cancel my appointment.')
    result = await booking.cancel_booking(db, new_call)
    writes_after = len([c for c in square.calls if c[0] == 'PUT' or c[1].endswith('/cancel')])
    assert writes_after == writes_before, ('UNRESOLVED MUTATION BYPASSED ON NEW CALL', result.outcome)
