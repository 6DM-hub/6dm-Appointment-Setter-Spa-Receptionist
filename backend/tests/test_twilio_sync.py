"""Tests for reconciling Twilio call history into `call_logs`.

The dashboard shows a call only if a correctly-scoped `call_logs` row exists, so
the risks worth pinning down are: misreading Twilio's `direction` values,
failing to recover our own number from a SIP URI, writing a row that no
`scope_filter` matches (invisible to every user), and clobbering a transcript
the live call flow already captured.
"""
import uuid
from datetime import datetime, timezone

import pytest

from app.models import CallDirection, CallStatus
from app.services.twilio_sync import _normalise


class _TwilioCall:
    """Stand-in for a twilio.rest call resource.

    Note the caller lands on `_from`, matching the real SDK instance — `from_`
    is only the `calls.create` keyword argument.
    """

    def __init__(self, **kw):
        self.sid = kw.get("sid", "CA" + uuid.uuid4().hex)
        self.direction = kw.get("direction", "inbound")
        self.status = kw.get("status", "completed")
        self._from = kw.get("from_", "+12149702434")
        self.to = kw.get("to", "+18058878345")
        self.forwarded_from = kw.get("forwarded_from")
        self.start_time = kw.get("start_time")
        self.end_time = kw.get("end_time")
        self.duration = kw.get("duration", "42")


# --------------------------------------------------------------------------- #
# Direction mapping
# --------------------------------------------------------------------------- #
def test_trunked_call_is_inbound_despite_twilios_naming():
    """Twilio calls a PSTN caller leaving over our trunk
    `trunking-originating`, named from the trunk's perspective. To the business
    it is an inbound call, and mislabelling it would file every SIP call under
    outbound sales."""
    record = _normalise(
        _TwilioCall(
            direction="trunking-originating",
            to="sip:+18058878345@sip.voice.x.ai;transport=tls",
            forwarded_from="+18058878345",
        )
    )
    assert record.direction is CallDirection.INBOUND


@pytest.mark.parametrize("raw", ["inbound", "trunking-originating"])
def test_inbound_directions(raw):
    assert _normalise(_TwilioCall(direction=raw)).direction is CallDirection.INBOUND


@pytest.mark.parametrize("raw", ["outbound-api", "outbound-dial", "trunking-terminating"])
def test_outbound_directions(raw):
    assert _normalise(_TwilioCall(direction=raw)).direction is CallDirection.OUTBOUND


def test_unmodelled_direction_is_skipped_not_guessed():
    assert _normalise(_TwilioCall(direction="something-new")) is None


# --------------------------------------------------------------------------- #
# Which of our numbers the call belongs to
# --------------------------------------------------------------------------- #
def test_dialed_number_comes_from_forwarded_from_on_trunked_calls():
    """`to` is a SIP URI on a trunked call, so tenant routing has to use
    `forwarded_from` — otherwise the call resolves to no spa and its row is
    written unowned."""
    record = _normalise(
        _TwilioCall(
            direction="trunking-originating",
            to="sip:+18058878345@sip.voice.x.ai;transport=tls",
            forwarded_from="+18058878345",
        )
    )
    assert record.dialed_number == "+18058878345"
    assert record.to_number == "+18058878345"


def test_dialed_number_falls_back_to_to_when_forwarded_from_absent():
    record = _normalise(_TwilioCall(direction="inbound", to="+18058878345"))
    assert record.dialed_number == "+18058878345"


def test_outbound_dialed_number_is_our_caller_id():
    record = _normalise(
        _TwilioCall(direction="outbound-api", from_="+18058878345", to="+12149702434")
    )
    assert record.dialed_number == "+18058878345"


def test_caller_id_is_read_from_the_sdks_underscore_from_attribute():
    """Regression: the JSON field is `from`, which the SDK exposes as `_from` on
    a call instance. Reading `from_` returns None, which turned every synced
    caller ID into "unknown" and made outbound calls unattributable."""
    call = _TwilioCall(direction="inbound", from_="+12149702434")
    assert not hasattr(call, "from_")  # mirrors the real resource
    assert _normalise(call).from_number == "+12149702434"


def test_caller_id_still_read_if_sdk_ever_exposes_from_underscore():
    class _Alt:
        sid, direction, status = "CAalt", "inbound", "completed"
        from_, to, forwarded_from = "+12149702434", "+18058878345", None
        start_time = end_time = None
        duration = "10"

    assert _normalise(_Alt()).from_number == "+12149702434"


def test_outbound_call_is_attributable_via_caller_id():
    """`dialed_number` is the tenant routing key; None means the call gets
    skipped as unattributable."""
    record = _normalise(
        _TwilioCall(direction="outbound-api", from_="+18058878345", to="+15550001111")
    )
    assert record.dialed_number == "+18058878345"


def test_unknown_numbers_do_not_become_a_routing_key():
    """'unknown' must not be used to look up a tenant — it would match nothing
    and silently produce an unowned row."""
    record = _normalise(_TwilioCall(direction="inbound", to=None, forwarded_from=None))
    assert record.to_number == "unknown"
    assert record.dialed_number is None


# --------------------------------------------------------------------------- #
# Status and timing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("completed", CallStatus.COMPLETED),
        ("busy", CallStatus.BUSY),
        ("failed", CallStatus.FAILED),
        ("no-answer", CallStatus.NO_ANSWER),
        ("canceled", CallStatus.CANCELLED),
        ("in-progress", CallStatus.IN_PROGRESS),
        ("ringing", CallStatus.RINGING),
    ],
)
def test_status_mapping(raw, expected):
    assert _normalise(_TwilioCall(status=raw)).status is expected


def test_unknown_status_defaults_to_queued_rather_than_crashing():
    assert _normalise(_TwilioCall(status="teleported")).status is CallStatus.QUEUED


def test_naive_timestamps_are_treated_as_utc():
    """Twilio hands back naive datetimes; storing them without a tzinfo against
    a timezone-aware column shifts every call by the server's offset."""
    record = _normalise(
        _TwilioCall(start_time=datetime(2026, 8, 31, 17, 11, 6), duration="7")
    )
    assert record.started_at == datetime(2026, 8, 31, 17, 11, 6, tzinfo=timezone.utc)
    assert record.duration_seconds == 7


def test_absent_duration_is_none_not_zero():
    """A zero would read as an answered call of no length; None means unknown."""
    assert _normalise(_TwilioCall(duration=None)).duration_seconds is None
    assert _normalise(_TwilioCall(duration="")).duration_seconds is None


def test_missing_timestamps_are_tolerated():
    record = _normalise(_TwilioCall(start_time=None, end_time=None))
    assert record.started_at is None and record.ended_at is None
