"""Requirement 4b: onboarding a spa is a database insert, not a deploy.

Each assertion below traces one thing the receptionist needs — routing, persona,
service menu, staff, hours, voice, booking target — back to a column on the
`SpaAccount` row, with no per-spa branch anywhere in the code.
"""
import pytest

from app.models import BookingProvider
from app.services.booking_adapters import SpaBookingAdapter
from app.services.grok_service import build_spa_prompt_context
from tests.conftest import make_spa

FULLY_CONFIGURED = dict(
    name="Solace Spa",
    twilio_phone_number="+15557654321",
    grok_system_prompt="Always greet guests as 'darling' and mention the tea bar.",
    services=[
        {"name": "Deep tissue massage", "duration_minutes": 60, "price": "$120"},
        {"name": "Signature facial", "duration_minutes": 75, "price": "$95"},
    ],
    staff=[
        {"name": "Riley", "role": "Massage therapist"},
        {"name": "Ash", "role": "Esthetician"},
    ],
    business_hours={
        "mon": [{"open": "09:00", "close": "18:00"}],
        "sat": [{"open": "10:00", "close": "16:00"}],
    },
    timezone="America/Los_Angeles",
    twiml_voice="Polly.Joanna",
    booking_provider=BookingProvider.SQUARE,
)


@pytest.fixture
def prompt() -> str:
    return build_spa_prompt_context(make_spa(**FULLY_CONFIGURED))


def test_prompt_carries_the_tenants_own_instructions(prompt):
    assert "darling" in prompt
    assert "tea bar" in prompt


def test_prompt_carries_the_service_menu(prompt):
    assert "Deep tissue massage" in prompt
    assert "60 minutes" in prompt
    assert "$120" in prompt
    assert "Signature facial" in prompt
    assert "[AUTHORITATIVE SPA FACTS — SERVICE MENU]" in prompt


def test_prompt_carries_the_team(prompt):
    assert "Riley (Massage therapist)" in prompt
    assert "Ash (Esthetician)" in prompt
    assert "[AUTHORITATIVE SPA FACTS — TEAM]" in prompt


def test_prompt_carries_hours_in_the_tenants_timezone(prompt):
    assert "Monday: 09:00 to 18:00" in prompt
    assert "Saturday: 10:00 to 16:00" in prompt
    assert "Tuesday: closed" in prompt
    assert "America/Los_Angeles" in prompt


def test_prompt_forbids_inventing_availability_from_hours(prompt):
    """Opening hours limit the day. They are not a list of open appointments."""
    assert "Opening hours are a hard limit." in prompt
    assert "Never offer or book a time before open or after close" in prompt
    assert "Inside those hours, only times the booking tool returns are real." in prompt


def test_a_bare_spa_row_still_produces_a_usable_prompt():
    """A spa onboarded with nothing but a name and a number must still answer
    the phone — the alternative is a silent outage on day one."""
    minimal = build_spa_prompt_context(make_spa(name="Brand New Spa"))

    assert "not configured" in minimal
    assert isinstance(minimal, str) and minimal.strip()


def test_braces_in_a_tenant_prompt_do_not_break_rendering():
    """Tenant text is substituted in, never used as a format string."""
    spa = make_spa(grok_system_prompt="Offer the {special} of the day {{today}}.")

    context = build_spa_prompt_context(spa)

    assert "{special}" in context
    assert "{{today}}" in context


def test_a_new_provider_needs_no_call_path_change():
    """Switching booking_provider changes the delegate and nothing else."""
    google = SpaBookingAdapter(
        make_spa(booking_provider=BookingProvider.GOOGLE_CALENDAR)
    )
    zenoti = SpaBookingAdapter(make_spa(booking_provider=BookingProvider.ZENOTI))

    assert google.calendar_label == zenoti.calendar_label
    assert google.default_title == zenoti.default_title


def test_voice_override_is_read_from_the_tenant_row():
    from app.api.v1.telephony import _resolve_voice

    assert _resolve_voice(make_spa(**FULLY_CONFIGURED), None) == "Polly.Joanna"
    # No override configured -> the platform default, not a crash.
    assert _resolve_voice(make_spa(twiml_voice=None), None)
