"""P0 #1: changing the SERVICE after a booking must reach Square, or be refused.

The production bug: a caller booked a Swedish massage at 2pm, then said "make
it the Signature Facial instead, same time". The confirm path compared only
start/end times, found them equal, and reported "Already confirmed" while
Square still held the massage. When the time also changed, only `start_at`
was sent, so Square kept the old service while the local row and the caller
were told otherwise.

These tests drive the real stage/confirm engine and the real Square adapter
against a stateful in-memory Square (`tests/square_world.py`) and assert on
what Square actually holds afterwards, not only on what CARA would say.
"""
from datetime import timedelta

import pytest

from app.models import Appointment, AppointmentStatus
from app.services import appointment_booking_service as svc
from app.services.appointment_booking_service import (
    BookingOutcome,
    confirm_booking,
    stage_booking,
)
from app.services.booking_adapters.base import AvailabilityVerdict, ExternalBooking
from app.services.booking_state import arm_verified_proposal, get_draft, stage
from app.services.grok_service import AppointmentIntent
from tests.square_world import THU_2PM_UTC, call, local, square_spa, wire

HOUR = timedelta(hours=1)


def _ask(service: str, start, name: str = "Ada Lovelace") -> AppointmentIntent:
    return AppointmentIntent(
        intent="schedule", confidence=1.0, caller_name=name,
        requested_start_iso=local(start), service_description=service,
    )


async def _stage_and_confirm(world, session, service, start):
    staged = await stage_booking(world.db, session, _ask(service, start))
    if staged.outcome is not BookingOutcome.DRAFT:
        return staged
    arm_verified_proposal(session)
    return await confirm_booking(world.db, session)


def _held(world):
    """(variation, team, start) of every active booking Square holds."""
    return sorted(
        (b["appointment_segments"][0]["service_variation_id"],
         b["appointment_segments"][0]["team_member_id"], b["start_at"])
        for b in world.square.active()
    )


@pytest.fixture
def world(monkeypatch):
    w = wire(monkeypatch)
    # Ana is the only massage therapist; Bea does facials and hot stone.
    w.square.open(THU_2PM_UTC, "var_swedish_60", "TM_ANA")
    w.square.open(THU_2PM_UTC, "var_facial_60", "TM_BEA")
    w.square.open(THU_2PM_UTC, "var_hotstone_90", "TM_BEA")
    w.square.open(THU_2PM_UTC + HOUR, "var_facial_60", "TM_ANA")
    return w


async def _booked_massage(world):
    session = call(world.spa)
    first = await _stage_and_confirm(world, session, "Swedish Massage", THU_2PM_UTC)
    assert first.outcome is BookingOutcome.BOOKED, first.message
    assert _held(world) == [("var_swedish_60", "TM_ANA", "2026-10-22T19:00:00Z")]
    return session, first.appointment


# --------------------------------------------------------------------------- #
# Square: the change is written and verified
# --------------------------------------------------------------------------- #
async def test_same_time_service_switch_is_written_to_square(world):
    session, row = await _booked_massage(world)

    result = await _stage_and_confirm(world, session, "Signature Facial", THU_2PM_UTC)

    assert result.outcome is BookingOutcome.RESCHEDULED, result.message
    assert "Already confirmed" not in result.message
    # Square now holds the facial on the SAME booking, not a second one.
    assert _held(world) == [("var_facial_60", "TM_BEA", "2026-10-22T19:00:00Z")]
    assert len(world.square.creates) == 1
    (path, body), = world.square.updates
    assert path == "/v2/bookings/sq_1"
    assert body["booking"]["appointment_segments"][0]["service_variation_id"] == "var_facial_60"
    # The local record matches what Square holds.
    assert world.store.live == [row]
    assert "Signature Facial" in row.title
    assert row.external_booking_id == "sq_1"


async def test_service_and_time_switch_in_one_request(world):
    session, row = await _booked_massage(world)

    result = await _stage_and_confirm(world, session, "Signature Facial", THU_2PM_UTC + HOUR)

    assert result.outcome is BookingOutcome.RESCHEDULED, result.message
    assert _held(world) == [("var_facial_60", "TM_ANA", "2026-10-22T20:00:00Z")]
    assert len(world.square.creates) == 1 and len(world.square.updates) == 1
    assert row.start_time == THU_2PM_UTC + HOUR
    assert "Signature Facial" in row.title


async def test_longer_service_does_not_inherit_the_old_end_time(world):
    session, row = await _booked_massage(world)

    result = await _stage_and_confirm(world, session, "Hot Stone Massage", THU_2PM_UTC)

    assert result.outcome is BookingOutcome.RESCHEDULED, result.message
    assert _held(world) == [("var_hotstone_90", "TM_BEA", "2026-10-22T19:00:00Z")]
    assert world.square.bookings["sq_1"]["appointment_segments"][0]["duration_minutes"] == 90
    assert row.end_time - row.start_time == timedelta(minutes=90)


async def test_repeated_confirmation_after_a_switch_writes_nothing_more(world):
    session, row = await _booked_massage(world)
    switched = await _stage_and_confirm(world, session, "Signature Facial", THU_2PM_UTC)
    assert switched.outcome is BookingOutcome.RESCHEDULED

    for _ in range(3):  # duplicated tool call, retried webhook, second "yes"
        arm_verified_proposal(session)
        again = await confirm_booking(world.db, session)
        assert again.outcome in {BookingOutcome.BOOKED, BookingOutcome.RESCHEDULED}
        assert again.appointment is row

    assert len(world.square.creates) == 1
    assert len(world.square.updates) == 1
    assert _held(world) == [("var_facial_60", "TM_BEA", "2026-10-22T19:00:00Z")]


# --------------------------------------------------------------------------- #
# Square: never report a change Square did not confirm
# --------------------------------------------------------------------------- #
async def test_square_keeping_the_old_service_is_not_reported_as_changed(world):
    session, row = await _booked_massage(world)
    world.square.ignore_segment_updates = True  # Square answers but keeps the massage

    result = await _stage_and_confirm(world, session, "Signature Facial", THU_2PM_UTC)

    assert result.outcome is not BookingOutcome.RESCHEDULED
    assert result.outcome is BookingOutcome.ERROR
    assert session.booking_status != "rescheduled"
    assert session.entities.get("booking_reconciliation_required")
    # The local record still describes what Square really holds.
    assert "Swedish Massage" in row.title
    assert row.start_time == THU_2PM_UTC


async def test_new_service_taken_before_confirm_changes_nothing(world):
    session, row = await _booked_massage(world)
    staged = await stage_booking(world.db, session, _ask("Signature Facial", THU_2PM_UTC))
    assert staged.outcome is BookingOutcome.DRAFT
    # Another caller books Bea for 2pm before this caller says yes.
    world.square.bookings["sq_other"] = {
        "id": "sq_other", "version": 1, "status": "ACCEPTED", "location_id": "loc_123",
        "start_at": "2026-10-22T19:00:00Z",
        "appointment_segments": [{"team_member_id": "TM_BEA", "service_variation_id": "var_facial_60",
                                  "service_variation_version": 5, "duration_minutes": 60}],
    }

    arm_verified_proposal(session)
    result = await confirm_booking(world.db, session)

    assert result.outcome is BookingOutcome.CONFLICT
    assert world.square.updates == []
    assert world.square.bookings["sq_1"]["appointment_segments"][0]["service_variation_id"] == "var_swedish_60"
    assert "Swedish Massage" in row.title


async def test_time_only_move_is_verified_against_what_square_stored(world):
    session, row = await _booked_massage(world)
    world.square.open(THU_2PM_UTC + 2 * HOUR, "var_swedish_60", "TM_ANA")
    world.square.wrong_start_on_update = True  # Square stores 30 minutes off

    result = await _stage_and_confirm(world, session, "Swedish Massage", THU_2PM_UTC + 2 * HOUR)

    assert result.outcome is BookingOutcome.ERROR
    assert session.booking_status != "rescheduled"
    assert row.start_time == THU_2PM_UTC, "local row must not claim the unverified time"


async def test_earlier_calls_booking_changed_to_a_longer_variation_reaches_square(monkeypatch):
    """60 -> 90 minutes on a booking made in an EARLIER call (no in-call record).

    The service names differ only by their length, so Square's own stored
    variation decides; the local row then follows what Square confirmed.
    """
    from tests.square_world import CATALOG
    menu = [item for item in CATALOG if item["name"].startswith("Swedish Massage ")]
    world = wire(monkeypatch, spa=square_spa(services=menu))
    world.square.open(THU_2PM_UTC, "var_swedish_60", "TM_ANA")
    world.square.open(THU_2PM_UTC + HOUR, "var_swedish_90", "TM_ANA")
    earlier = call(world.spa, "CA-yesterday")
    booked = await _stage_and_confirm(world, earlier, "Swedish Massage 60", THU_2PM_UTC)
    assert booked.outcome is BookingOutcome.BOOKED, booked.message
    row = booked.appointment

    today = call(world.spa, "CA-today")
    draft = get_draft(today)
    draft.operation_mode = "reschedule"
    draft.target_appointment_id = str(row.id)
    draft.offered_appointment_ids = [str(row.id)]
    from app.services.booking_state import save_draft
    save_draft(today, draft)
    staged = await stage_booking(world.db, today, AppointmentIntent(
        intent="reschedule", confidence=1.0, caller_name="Ada Lovelace",
        requested_start_iso=local(THU_2PM_UTC + HOUR), service_description="Swedish Massage 90",
    ))
    assert staged.outcome is BookingOutcome.DRAFT, staged.message
    arm_verified_proposal(today)
    result = await confirm_booking(world.db, today)

    assert result.outcome is BookingOutcome.RESCHEDULED, result.message
    assert _held(world) == [("var_swedish_90", "TM_ANA", "2026-10-22T20:00:00Z")]
    assert "90" in row.title
    assert row.end_time - row.start_time == timedelta(minutes=90)
    assert len(world.square.creates) == 1


# --------------------------------------------------------------------------- #
# Other calendars
# --------------------------------------------------------------------------- #
class _Calendar:
    """A non-Square calendar with a configurable service-change capability."""

    calendar_label = "the test calendar"
    default_title = "Appointment"
    default_duration_minutes = 60

    def __init__(self, provider: str, mode: str) -> None:
        self.provider = provider
        self.service_change_mode = mode
        self.created: list = []
        self.moved: list = []

    async def booking_timezone_name(self):
        return None

    async def list_openings(self, ctx, range_start, range_end):
        return []

    async def check_availability(self, ctx):
        return AvailabilityVerdict.ok(slot={
            "start": ctx.start.isoformat(),
            "duration_minutes": int((ctx.end - ctx.start).total_seconds() / 60),
        })

    async def create_booking(self, ctx):
        self.created.append(ctx)
        return ExternalBooking(provider=self.provider, external_id=f"evt-{len(self.created)}")

    async def move_booking(self, booking, start, end):
        self.moved.append((booking.external_id, start, end))
        return booking

    async def cancel_booking(self, booking):
        return None


async def test_calendar_that_cannot_switch_service_refuses_and_keeps_booking(monkeypatch):
    adapter = _Calendar("google_calendar", "unsupported")
    world = wire(monkeypatch, spa=square_spa(), adapter=adapter)
    session = call(world.spa)
    first = await _stage_and_confirm(world, session, "Swedish Massage", THU_2PM_UTC)
    assert first.outcome is BookingOutcome.BOOKED

    result = await _stage_and_confirm(world, session, "Signature Facial", THU_2PM_UTC)

    assert result.outcome is BookingOutcome.CONFLICT
    assert "Nothing was changed" in result.message
    assert adapter.moved == [] and len(adapter.created) == 1
    assert "Swedish Massage" in first.appointment.title


async def test_cara_native_service_switch_updates_the_booking_of_record(monkeypatch):
    adapter = _Calendar("cara_native", "local")
    world = wire(monkeypatch, spa=square_spa(), adapter=adapter)
    session = call(world.spa)
    first = await _stage_and_confirm(world, session, "Swedish Massage", THU_2PM_UTC)
    assert first.outcome is BookingOutcome.BOOKED

    result = await _stage_and_confirm(world, session, "Signature Facial", THU_2PM_UTC)

    assert result.outcome is BookingOutcome.RESCHEDULED, result.message
    assert world.store.live == [first.appointment]
    assert "Signature Facial" in first.appointment.title
    assert len(adapter.created) == 1


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("left,right,same", [
    ("60 minute deep tissue massage", "Deep tissue massage", True),
    ("Swedish Massage 90 (90 min)", "Swedish Massage (60 min)", False),
    ("90 minute deep tissue", "Deep Tissue Massage (90 min)", True),
    ("Swedish Massage (60 min)", "swedish massage", True),
    ("Signature Facial (60 min)", "Swedish Massage (60 min)", False),
    ("Hot Stone Massage", "Swedish Massage", False),
])
def test_service_family_comparison(left, right, same):
    assert svc._same_service_family(left, right) is same


def test_switching_service_drops_the_previous_services_end_time():
    from app.services.call_state import CallSession

    session = CallSession("CA-unit", "inbound", "+1555", "+1556")
    stage(session, AppointmentIntent(
        intent="schedule", confidence=1.0, requested_start_iso="2026-10-22T14:00:00",
        requested_end_iso="2026-10-22T15:00:00", service_description="Swedish Massage",
    ))
    stage(session, AppointmentIntent(
        intent="schedule", confidence=1.0, requested_start_iso="2026-10-22T14:00:00",
        service_description="Hot Stone Massage",
    ))
    assert get_draft(session).end_iso is None
