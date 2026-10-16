"""Same cancellation contract with real PostgreSQL locking and refresh."""
import pytest
from app.models import AppointmentStatus
from tests.test_brick1_terminal_cancellation import check_terminal_cancel
from tests_postgres.test_postgres_booking_races import pg  # noqa: F401


@pytest.mark.parametrize("terminal", [AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW])
@pytest.mark.parametrize("method", ["patch", "delete"])
async def test_terminal_cancel_is_refused_without_writes(pg, monkeypatch, terminal, method):
    maker, spas = pg
    async with maker() as db:
        await check_terminal_cancel(db, spas[0], monkeypatch, terminal, method)
