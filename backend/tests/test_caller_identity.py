import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.tenancy import TenantScope
from app.models import CallLog, Contact
from app.services.call_state import CallSession
from app.services.caller_identity import persist_caller_identity


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


@pytest.mark.asyncio
async def test_new_spa_caller_is_created_and_call_is_linked():
    spa_id = uuid.uuid4()
    call_sid = "CA-new"
    call_log = CallLog(
        id=uuid.uuid4(), twilio_call_sid=call_sid, tenant_id=spa_id,
        from_number="+15551234567", to_number="+15550000001",
    )
    db = AsyncMock()
    db.add = Mock()
    db.execute = AsyncMock(side_effect=[_Result(None), _Result(call_log)])

    async def flush_new_contact():
        db.add.call_args.args[0].id = uuid.uuid4()

    db.flush = AsyncMock(side_effect=flush_new_contact)

    session = CallSession(
        call_sid, "inbound", "+1 (555) 123-4567", "+15550000001",
        tenant_id=str(spa_id),
    )
    contact = await persist_caller_identity(db, session, "Sarah Johnson")

    assert contact.full_name == "Sarah Johnson"
    assert contact.tenant_id == spa_id
    assert contact.phone_number == "+15551234567"
    assert call_log.contact_id == contact.id


@pytest.mark.asyncio
async def test_existing_unnamed_spa_contact_is_updated_not_duplicated():
    spa_id = uuid.uuid4()
    contact = Contact(
        id=uuid.uuid4(), tenant_id=spa_id, phone_number="+15551234567"
    )
    call_log = CallLog(
        id=uuid.uuid4(), twilio_call_sid="CA-existing", tenant_id=spa_id,
        from_number="+15551234567", to_number="+15550000001",
    )
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_Result(contact), _Result(call_log)])

    session = CallSession(
        "CA-existing", "inbound", "+15551234567", "+15550000001",
        tenant_id=str(spa_id),
    )
    result = await persist_caller_identity(db, session, "Sarah Johnson")

    assert result is contact
    assert contact.full_name == "Sarah Johnson"
    assert call_log.contact_id == contact.id
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_same_phone_is_resolved_inside_each_spa_scope():
    phone = "+15551234567"
    spa_a, spa_b = uuid.uuid4(), uuid.uuid4()
    contact_a = Contact(id=uuid.uuid4(), tenant_id=spa_a, phone_number=phone, first_name="A")
    contact_b = Contact(id=uuid.uuid4(), tenant_id=spa_b, phone_number=phone, first_name="B")

    for spa_id, expected in ((spa_a, contact_a), (spa_b, contact_b)):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_Result(expected), _Result(None)])
        session = CallSession(str(spa_id), "inbound", phone, "+15550000001", tenant_id=str(spa_id))
        result = await persist_caller_identity(db, session, None)
        assert result is expected