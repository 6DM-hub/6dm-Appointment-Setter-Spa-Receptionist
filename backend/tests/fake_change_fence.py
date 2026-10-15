"""In-memory stand-in for the write-ahead change fence, for the FakeDB worlds.

The FakeDB used by the voice-path suites has no real engine, so the fence
(which commits on its own connection) is modelled on the same in-memory
``unresolved`` list those worlds already use for durable unresolved-change
records: an open fence is a row that blocks later changes of the appointment,
a verified outcome removes it, an unknown outcome marks it. Real database
behaviour is covered on SQLite (tests/test_brick1_repair3.py, Codex's R3
cases) and PostgreSQL (tests_postgres/).
"""
import uuid


def install(monkeypatch, svc, unresolved: list) -> None:
    from app.services.booking_escalations import ChangeInFlight

    async def _open(_db, tenant_id, call_reference, details, *, fence_id=None):
        if any(str(row.get("appointment_id")) == str(details.get("appointment_id")) for row in unresolved):
            raise ChangeInFlight(details.get("appointment_id"))   # exclusive, as the real fence
        fence_id = fence_id or uuid.uuid4()
        unresolved.append({"fence_id": str(fence_id), "tenant_id": tenant_id,
                           "call_reference": call_reference, "stage": "in_flight", **details})
        return fence_id

    async def _close(_db, fence_id, *, own_transaction=False):
        unresolved[:] = [row for row in unresolved if row.get("fence_id") != str(fence_id)]

    async def _mark(_db, fence_id, details):
        for row in unresolved:
            if row.get("fence_id") == str(fence_id):
                row.update(details, stage="unknown_outcome", fence_id=str(fence_id))
                return row
        return None

    monkeypatch.setattr(svc, "_open_change_fence", _open)
    monkeypatch.setattr(svc, "_close_change_fence", _close)
    monkeypatch.setattr(svc, "_mark_change_unknown", _mark)
