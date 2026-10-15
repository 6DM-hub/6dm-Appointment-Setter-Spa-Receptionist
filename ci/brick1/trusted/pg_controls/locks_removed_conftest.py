import pytest
from app.services import appointment_booking_service as booking
@pytest.fixture(autouse=True)
def no_locks(monkeypatch):
    async def noop(*a, **k): return None
    monkeypatch.setattr(booking, "_lock_slot", noop)
    monkeypatch.setattr(booking, "_lock_call", noop)
