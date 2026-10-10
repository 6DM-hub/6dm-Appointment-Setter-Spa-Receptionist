"""Live-call regressions: natural day queries, cancellation and response races."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import xai_realtime as realtime
from app.services import appointment_booking_service as booking
from app.services.booking_state import get_draft, save_draft, start_new_intent
from app.services.call_state import CallSession
from app.services.xai_realtime import XAIVoiceSession
from tests.test_booking_intent_state import world, _confirm, _wants, SEPT_22


def voice():
    session = CallSession("retry", "inbound", "+15550000001", "+15550000002", timezone="America/Chicago")
    return XAIVoiceSession("retry", session)


@pytest.mark.parametrize("question", ["What availability do you have today?", "What times are available tomorrow?", "Any openings today?"])
def test_natural_day_request_is_a_bounded_local_window(question):
    v = voice()
    v.session.add_turn("user", question)
    start, end = v._spoken_day_part_window()
    assert start.tzinfo == v._tz
    assert end.hour == 0
    assert timedelta(0) < end - start <= timedelta(days=1)
    assert end.date() == datetime.now(v._tz).date() + timedelta(days=2 if "tomorrow" in question else 1)


def test_unspecified_date_does_not_authorize_arbitrary_day_search():
    v = voice()
    v.session.add_turn("user", "I'd like a massage")
    assert v._spoken_day_part_window() is None


def test_clarified_turn_resets_guard_cache_and_callback_without_consent():
    v = voice()
    v._availability_recent["old"] = (1, "unavailable")
    v._rejected_probe_signatures_this_turn.add(("propose_appointment", "earliest"))
    v.session.entities["callback_offer_pending"] = True
    v._pending_caller = "What availability do you have today?"
    v._flush_caller_turn()
    assert not v._availability_recent
    assert not v._rejected_probe_signatures_this_turn
    assert not v.session.entities.get("callback_offer_pending")
    assert not v.session.entities.get("callback_authorized")


async def test_old_response_done_does_not_close_newer_response():
    v = voice()
    v._active_response_id = "new"
    await v._dispatch({"type": "response.done", "response": {"id": "old"}})
    assert v._active_response_id == "new"


async def test_cancel_race_does_not_replay_greeting_or_mutate_new_response():
    v = voice()
    v._active_response_id = "new"
    v._greet_via_model = AsyncMock()
    await v._dispatch({"type": "error", "error": {"message": "Cancellation failed: no active response found"}})
    assert v._active_response_id == "new"
    v._greet_via_model.assert_not_awaited()


async def test_cancel_only_sent_once_for_active_response():
    v = voice()
    v._send = AsyncMock()
    v._active_response_id = "one"
    await v._cancel_active_response()
    await v._cancel_active_response()
    assert v._send.await_count == 1


def test_rebook_same_cancelled_time_authorizes_fresh_lookup_only():
    v = voice()
    start = datetime.now(v._tz) + timedelta(days=1)
    v.session.entities["last_cancelled_appointment"] = {"start_iso": start.isoformat(), "service": "60 minute Swedish massage"}
    v.session.add_turn("user", "Can I rebook that same time?")
    assert v._is_time_grounded(start.isoformat())
    assert not v._is_time_grounded((start + timedelta(minutes=30)).isoformat())
    assert not get_draft(v.session).provider_verified
    assert not get_draft(v.session).confirmation_authorized
    v.session.add_turn("user", "Actually I'd like tomorrow at 5 pm")
    assert v._cancelled_slot_reference() is None


@pytest.mark.parametrize("outcome,cleared", [(booking.BookingOutcome.CANCELLED, True), (booking.BookingOutcome.ERROR, False)])
async def test_voice_cache_invalidation_requires_successful_cancellation(monkeypatch, outcome, cleared):
    v = voice()
    v._availability_recent["slot"] = (1, "unavailable")
    v._rejected_probe_signatures_this_turn.add(("probe", "slot"))
    v._persist_session = AsyncMock()
    monkeypatch.setattr(realtime, "cancel_booking", AsyncMock(return_value=booking.BookingResult(outcome, message="result")))
    await v._run_cancel_appointment("{}")
    assert (not v._availability_recent) is cleared
    assert (not v._rejected_probe_signatures_this_turn) is cleared


async def test_cancel_then_rebook_same_time_is_a_new_idempotent_booking(world):
    db, session = world["db"], world["session"]
    first = await _confirm(db, session, _wants(SEPT_22))
    assert first.outcome == booking.BookingOutcome.BOOKED
    cancelled = await booking.cancel_booking(db, session)
    assert cancelled.outcome == booking.BookingOutcome.CANCELLED
    second = await _confirm(db, session, _wants(SEPT_22))
    assert second.outcome == booking.BookingOutcome.BOOKED
    assert second.appointment.id != first.appointment.id
    repeated = await _confirm(db, session)
    assert repeated.appointment.id == second.appointment.id
    assert len(world["store"].live) == 1
    assert len(world["adapter"].created) == 2


async def test_overlapping_slots_share_scope_lock_but_tenants_do_not():
    db = SimpleNamespace(bind=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")), execute=AsyncMock())
    start = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)
    await booking._lock_slot(db, SimpleNamespace(tenant_id="a", owner_id="owner"), start, start + timedelta(hours=1))
    await booking._lock_slot(db, SimpleNamespace(tenant_id="a", owner_id="owner"), start + timedelta(minutes=30), start + timedelta(hours=2))
    await booking._lock_slot(db, SimpleNamespace(tenant_id="b", owner_id="owner"), start, start + timedelta(hours=1))
    keys = [call.args[1]["key"] for call in db.execute.await_args_list]
    assert keys[0] == keys[1]
    assert keys[0] != keys[2]


def test_new_intent_invalidates_inflight_lookup_generation():
    v = voice()
    old = booking._availability_generation(get_draft(v.session))
    start_new_intent(v.session)
    assert old != booking._availability_generation(get_draft(v.session))


async def test_day_query_asks_for_missing_service_without_provider_lookup(monkeypatch):
    v = voice()
    prepare = AsyncMock()
    monkeypatch.setattr(booking, "_prepare", prepare)
    intent = realtime.AppointmentIntent(confidence=1, intent="schedule")
    start = datetime.now(v._tz)
    result = await booking.search_day_part(None, v.session, intent, start, start + timedelta(hours=2))
    assert result.outcome == booking.BookingOutcome.MISSING_INFO
    assert "service" in result.message
    prepare.assert_not_awaited()


def test_service_clarification_retains_requested_day():
    v = voice()
    start = datetime.now(v._tz)
    end = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    v.session.entities["requested_availability_window"] = [start.isoformat(), end.isoformat()]
    v.session.add_turn("user", "A 60 minute Swedish massage")
    retained_start, retained_end = v._spoken_day_part_window()
    assert retained_start.date() == start.date()
    assert retained_end == end


async def test_day_lookup_cannot_restore_slots_after_cancellation_or_new_intent(world, monkeypatch):
    db, session = world["db"], world["session"]
    async def delayed_list(*args):
        start_new_intent(session)
        return [{"start": SEPT_22.isoformat(), "duration_minutes": 60, "team_member_id": "TM_1"}]
    monkeypatch.setattr(world["adapter"], "list_openings", delayed_list)
    result = await booking.search_day_part(db, session, _wants(SEPT_22), SEPT_22, SEPT_22 + timedelta(hours=2))
    assert result.outcome == booking.BookingOutcome.SKIPPED
    assert not get_draft(session).alternative_slots


async def test_time_taken_at_final_recheck_does_not_create_booking(world):
    db, session, adapter = world["db"], world["session"], world["adapter"]
    staged = await booking.stage_booking(db, session, _wants(SEPT_22))
    assert staged.outcome == booking.BookingOutcome.DRAFT
    adapter.busy.append((SEPT_22, SEPT_22 + timedelta(hours=1)))
    result = await _confirm(db, session)
    assert result.outcome == booking.BookingOutcome.CONFLICT
    assert result.message.startswith("It looks like that time was just taken")
    assert not adapter.created
    assert not world["store"].live


async def test_missing_service_day_query_asks_once_and_resumes_after_clarification(monkeypatch):
    v = voice()
    v._persist_session = AsyncMock()
    v._send_function_output = AsyncMock()
    v._deliver_authoritative_availability = AsyncMock()
    v._arm_availability_hold = AsyncMock()
    start = datetime.now(v._tz)
    end = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    await v._offer_spoken_window("query-1", (start, end), {})
    await v._offer_spoken_window("query-2", (start, end), {})
    assert v._deliver_authoritative_availability.await_count == 1
    output = json.loads(v._deliver_authoritative_availability.await_args.args[1])
    assert output["status"] == "missing_info"
    assert output["spoken"] == "Which service would you like, and for how many minutes?"
    v._arm_availability_hold.assert_not_awaited()
    v._pending_caller = "A 60 minute Swedish mÙ™™\—ØXØÙ\Y
›ÚXÙKœÙ\ÜÚ[ÛŠBˆ\ÜÙ\›ÝÙ]Ù˜Y
›ÚXÙKœÙ\ÜÚ[ÛŠK˜ÛÛ™š\›X][Û—Ø]]Üš^™Yˆ\ÜÙ\›ÚXÙKœÜÚÙ[ˆOH×B‚‚]\Ý›X\šË˜\Þ[˜Ú[Â˜\Þ[˜ÈYˆ\ÝÜ™\ÜYÚ[œÝXÝ[Û—ÛXZ×Ú\×ØØ[˜Ù[Y

N‚ˆ›ÚXÙHH[™[™×Ý›ÚXÙJ
Bˆ]ØZ]›ÚXÙK—Ù\Ü]Ú
È\HŽˆœ™\ÜÛœÙK˜]Y[×Ý˜[œØÜš\™[H‹ˆ™[HŽˆ’HÛÛ‰ÝÚ]™H[žHÝ\ˆ[Y\È›Üˆ\È\Ú[Y[ˆŸJBˆ\ÜÙ\•Ú]ÛÝ[[ÝHZÙHÈÏÈˆ[ˆÝŠ›ÚXÙKœÜÚÙ[ŠBˆ\ÜÙ\›Ý\ˆ[Y\Èˆ›Ý[ˆÝŠ›ÚXÙKœÜÚÙ[ŠB‚‚™Yˆ\ÝÜ›ÝšY\—ÜÝXØÙ\Ü×ØÛÛ™š\›X][Û—Ú\×Û›ÝÜÜÚÙ[—ÝÚXÙJ
N‚ˆ›ÚXÙHH[™[™×Ý›ÚXÙJ
Bˆ^[ØYHœÛÛ‹™[\ÊÈœÝ]\ÈŽˆ˜›ÛÚÙY‹˜\Ú[Y[ÚYŽˆ›ØØ[]\Ý‹ˆ™^\›˜[Ø›ÛÚÚ[™×ÚYŽˆœ›ÝšY\‹]\ÝŸJBˆ\ÜÙ\›ÚXÙK—Ø]]Üš]]]™WÝÛÛÙ›ÛÝÝ\
˜ÛÛ™š\›WØ\Ú[Y[‹^[ØY
Bˆ\ÜÙ\›ÚXÙK—Ø]]Üš]]]™WÝÛÛÙ›ÛÝÝ\
˜ÛÛ™š\›WØ\Ú[Y[‹^[ØY
HOHˆ‚‚‚]\Ý›X\šË˜\Þ[˜Ú[Â˜\Þ[˜ÈYˆ\ÝÙ\XØ]WÜ›ÜÜØ[Ü™]\Ù\×Ý™\šYšYYÜ™\Ý[
[ÛšÙ^\]Ú
N‚ˆ›ÚXÙHH[™[™×Ý›ÚXÙJ
Bˆ›ÚXÙK—ÝˆH›Û™R[™›Ê[Y\šXØKÐÚXØYÛÈŠBˆ\Þ[˜ÈYˆ™Z™XÝ

˜\™ÜÊN‚ˆ˜Z\ÙH\ÜÙ\[Û‘\œ›ÜŠšY[]HÛÛXÝ[Ûˆ]\Ý›Ý™\Ý\]˜Z[Xš[]HŠBˆ[ÛšÙ^\]ÚœÙ]]Š˜\œÙ\šXÙ\ËžZWÜ™X[[YKœÝYÙWØ›ÛÚÚ[™È‹™Z™XÝ
BˆÝ]]HœÛÛ‹›ØYÊ]ØZ]›ÚXÙK—Ü[—Ü›ÜÜÙWØ\Ú[Y[
œÛÛ‹™[\ÊÂˆœ™\]Y\ÝYÜÝ\Ú\ÛÈŽˆÙ]Ù˜Y
›ÚXÙKœÙ\ÜÚ[ÛŠKœÝ\Ú\ÛËˆœÙ\šXÙWÙ\ØÜš\[ÛˆŽˆŽLZ[]HY\\ÜÝYH‹›Ü\˜][ÛˆŽˆœØÚY[HŸJJJBˆ\ÜÙ\Ý]]ÈœÝ]\È—HOH™˜Y‚ˆ\ÜÙ\Ý]]ÈœÜÚÙ[ˆ—H\È›Û™B‚‚]\Ý›X\šË˜\Þ[˜Ú[Â˜\Þ[˜ÈYˆ\ÝÜÜ]X\™WÛ™]×Ú[[Ø]›ÚY×ØØ[˜Ù[YÜ™\^WØ[™Ü™]žWÚÙ^WÚ\×ÜÝX›J
N‚ˆY\\ˆHÜÜ]X\™WØY\\ŠXZÙWÜÜJ›ÛÚÚ[™×Ü›ÝšY\P›ÛÚÚ[™Ô›ÝšY\‹”ÔUPT‘Kˆ›ÛÚÚ[™×ØÛÛ™šYÏ^È˜XØÙ\Ü×ÝÚÙ[ˆŽˆ\Ý‹›ØØ][Û—ÚYŽˆ›Ø×ÌLŒÈŸJJBˆ˜[œÜÜHÑ˜ZÙTÜ]X\™U˜[œÜÜ

Bˆ˜[œÜÜ˜Ø][Ù×Ú][\ÈHÞÈš][WÙ]HŽˆÈ›˜[YHŽˆ‘Y\\ÜÝYHX\ÜØYÙH‹˜\šX][ÛœÈŽˆÂˆÈšYŽˆ˜\ŒH‹™\œÚ[ÛˆŽˆš][WÝ˜\šX][Û—Ù]HŽˆÈ›˜[YHŽˆ”™YÝ[\ˆŸ_W__WBˆ˜[œÜÜ˜]˜Z[Xš[]Y\ÈHÞÈœÝ\Ø]ŽˆŒŒ‹LKLNNŒŒˆ‹›ØØ][Û—ÚYŽˆ›Ø×ÌLŒÈ‹ˆ˜\Ú[Y[ÜÙYÛY[ÈŽˆÞÈX[WÛY[X™\—ÚYŽˆœÝY™ˆ‹œÙ\šXÙWÝ˜\šX][Û—ÚYŽˆ˜\ŒH‹ˆœÙ\šXÙWÝ˜\šX][Û—Ý™\œÚ[ÛˆŽˆ™\˜][Û—ÛZ[]\ÈŽˆŒW_WBˆ˜[œÜÜ˜Ý\ÝÛY\œÈHÞÈšYŽˆ˜Ý\ÝÌHŸWBˆY\\‹—Ü™\]Y\ÝH˜[œÜÜˆ›Üˆ™Y™\™[˜ÙH[ˆÈ›ÛZ[[‹›™]ËZ[[‹›™]ËZ[[—N‚ˆ]ØZ]Y\\‹˜Ü™X]WØ›ÛÚÚ[™ÊØÝ
›ÛÚÚ[™×Ü™Y™\™[˜ÙO\™Y™\™[˜ÙJJBˆÙ^\ÈHÚ][VÈšY[\Ý[˜ÞWÚÙ^H—H›Üˆ][H[ˆ˜[œÜÜ˜Ø[×ÝÊ‹ÝŒ‹Ø›ÛÚÚ[™ÜÈŠWBˆ\ÜÙ\Ù^\ÖÌHOHÙ^\ÖÌWBˆ\ÜÙ\Ù^\ÖÌWHOHÙ^\ÖÌ—B‚‚™Yˆ\ÝÜ›Û\Üš[Üš]^™\×ØXØÙ\[˜ÙWØ[™Ý™\šYšYYÛÜ[Û˜[Ù[š[˜Ù[Y[Ê
N‚ˆ›Û\HZ[Ü™X[[YWÚ[œÝXÝ[ÛœÊ•\ÝÜH‹[Ø^\È\ÚÈ›ÜˆÛÛ™š\›X][ÛˆÚXÙHŠBˆ\ÜÙ\›Û\š[™^
“ÓÒÒS‘ÈÓÓ•‘T”ÐUSÓˆ•STÈŠHˆ›Û\š[™^
[Ø^\È\ÚÈŠBˆ\ÜÙ\“™]™\ˆ\ÚÈ[HÈ\›Ý™HHØ[YH]KÝ[YHYØZ[ˆˆ[ˆ›Û\ˆ\ÜÙ\™\šYšYYY[Hˆ[ˆ›Û\ˆ\ÜÙ\‘È›Ý[™[šXÙ\Èˆ[ˆ›Û\