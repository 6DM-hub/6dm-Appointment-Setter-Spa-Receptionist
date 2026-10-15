"""CONTROL ONLY: disable the write-ahead change fence (opening it records
nothing), to show which PostgreSQL results depend on it."""
import pytest


@pytest.fixture(autouse=True)
def _no_change_fence(monkeypatch):
    from app.services import appointment_booking_service as booking

    async def _none(*args, **kwargs):
        return None

    monkeypatch.setattr(booking, "_open_change_fence", _none)
