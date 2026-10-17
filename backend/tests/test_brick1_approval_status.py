"""The appointment contract can represent a received, unapproved request."""
from app.models import AppointmentStatus
from app.schemas import AppointmentUpdate


def test_awaiting_approval_survives_the_appointment_contract():
    payload = AppointmentUpdate.model_validate({"status": "awaiting_provider_approval"})
    assert payload.status.value == "awaiting_provider_approval"
    assert payload.status not in {AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED}
