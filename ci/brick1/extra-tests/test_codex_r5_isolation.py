"""Independent R5 isolation/concurrent transcript candidates. UNEXECUTED.

This does not substitute for PostgreSQL transaction/race tests. It exercises
concurrent realtime sessions against the supplied in-memory provider harness.
"""
import asyncio
import pytest
from app.services import appointment_booking_service as booking
from app.services.booking_state import get_draft, save_draft
from tests.test_cara_native_double_booking import rubyz_db
from tests.test_codex_brick1_tenant_boundaries import setup_tenants, session_for, book
from tests.test_brick1_booking_change_guard import Phone, make_world, T2, T3, local
from tests.square_world import call

@pytest.mark.parametrize('foreign_tenant', [True, False])
async def test_contaminated_offer_cannot_cancel_another_customer(rubyz_db, monkeypatch, foreign_tenant):
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    victim = await book(db, a, session_for(a, 'r5-victim'))
    attacker = session_for(b if foreign_tenant else a, 'r5-attacker', phone='+15550009999')
    draft = get_draft(attacker)
    draft.offered_appointment_ids = [str(victim.id)]
    save_draft(attacker, draft)
    attacker.add_turn('user', 'Please cancel my appointment.')
    await booking.cancel_booking(db, attacker, appointment_id=str(victim.id))
    attacker.add_turn('user', 'Yes.')
    await booking.cancel_booking(db, attacker, appointment_id=str(victim.id))
    assert len(squares['fake-cleaning-token'].active()) == 1
    assert not any(route.endswith('/cancel') for square in squares.values() for _, route, _ in square.calls)

async def test_concurrent_realtime_yes_and_duplicate_tools_preserve_each_offer(monkeypatch):
    w = make_world(monkeypatch, 'hvac')
    a = Phone(w, call(w.spa, sid='CA-r5-a'))
    b = Phone(w, call(w.spa, sid='CA-r5-b'))
    for p, start, name in [(a, T2, 'Ada Lovelace'), (b, T3, 'Grace Hopper')]:
        await p.says(f'A {w.first} on Thursday. My name is {name}.')
        result = await p.propose(operation='schedule', requested_start_iso=local(start), service_description=w.first, caller_name=name)
        assert result['status'] == 'draft'
    await asyncio.gather(a.says('Yes.'), b.says('Yes.'))
    assert len(w.square.creates) == 2
    await asyncio.gather(a.tool('confirm_appointment'), b.tool('confirm_appointment'))
    assert len(w.square.creates) == 2 and not w.square.updates
    assert sorted(x['start_at'] for x in w.square.active()) == [T2.strftime('%Y-%m-%dT%H:%M:%SZ'), T3.strftime('%Y-%m-%dT%H:%M:%SZ')]
