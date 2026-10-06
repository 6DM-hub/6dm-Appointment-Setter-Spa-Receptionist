"""Row-level scoping: the filter that keeps one spa out of another's data."""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

from app.schemas.contact import ContactRead

from app.api.deps import get_tenant_scope
from app.core.tenancy import TenantScope, call_log_scope_filter, owns, scope_columns, scope_filter
from app.main import app
from app.models import Appointment, CallLog, Contact

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
OWNER = uuid.uuid4()


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize("model", [Contact, Appointment, CallLog])
def test_tenant_scope_filters_on_tenant_id(model):
    clause = _sql(scope_filter(TenantScope.for_tenant(TENANT_A), model))
    assert "tenant_id = " in clause
    assert "owner_id" not in clause and "user_id" not in clause


@pytest.mark.parametrize(
    "model,owner_column",
    [(Contact, "owner_id"), (Appointment, "user_id"), (CallLog, "user_id")],
)
def test_sales_scope_requires_null_tenant_and_matching_owner(model, owner_column):
    clause = _sql(scope_filter(TenantScope.for_sales_workspace(OWNER), model))
    assert "tenant_id IS NULL" in clause
    assert owner_column in clause


def test_spa_call_scope_is_inbound_only():
    clause = _sql(call_log_scope_filter(TenantScope.for_tenant(TENANT_A), CallLog))
    assert "tenant_id = " in clause
    assert "direction = " in clause


def test_sales_call_scope_keeps_outbound_calls_available():
    clause = _sql(call_log_scope_filter(TenantScope.for_sales_workspace(OWNER), CallLog))
    assert "direction" not in clause


@pytest.mark.parametrize(
    "model,owner_column",
    [(Contact, "owner_id"), (Appointment, "user_id")],
)
def test_scope_columns_set_exactly_one_owner(model, owner_column):
    """Matches the `num_nonnulls(...) = 1` check constraint on both tables."""
    assert scope_columns(TenantScope.for_tenant(TENANT_A), model) == {
        "tenant_id": TENANT_A
    }
    assert scope_columns(TenantScope.for_sales_workspace(OWNER), model) == {
        owner_column: OWNER
    }


def test_owns_rejects_rows_from_another_tenant():
    guest = Contact(tenant_id=TENANT_A, owner_id=None, phone_number="+15550001")
    lead = Contact(tenant_id=None, owner_id=OWNER, phone_number="+15550002")

    assert owns(TenantScope.for_tenant(TENANT_A), guest)
    assert not owns(TenantScope.for_tenant(TENANT_B), guest)
    assert not owns(TenantScope.for_tenant(TENANT_A), lead)

    assert owns(TenantScope.for_sales_workspace(OWNER), lead)
    assert not owns(TenantScope.for_sales_workspace(OWNER), guest)
    assert not owns(TenantScope.for_sales_workspace(uuid.uuid4()), lead)


def test_contact_blank_email_is_normalized_to_none():
    row = ContactRead.model_validate({
        "id": uuid.uuid4(),
        "owner_id": None,
        "tenant_id": TENANT_A,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "phone_number": "+15550000001",
        "email": "",
        "extra_metadata": {},
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "full_name": "Ada Lovelace",
    })

    assert row.email is None
    assert row.full_name == "Ada Lovelace"


def test_contact_read_does_not_fail_the_guest_list_on_messy_receptionist_data():
    """One bad stored email/phone must not 500 GET /contacts."""
    row = ContactRead.model_validate({
        "id": uuid.uuid4(),
        "owner_id": None,
        "tenant_id": TENANT_A,
        "first_name": "Sarah",
        "last_name": None,
        "phone_number": "555-0199",
        "email": "sarah at the spa",
        "extra_metadata": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "full_name": "Sarah",
    })
    assert row.email == "sarah at the spa"
    assert row.phone_number == "555-0199"
    assert row.extra_metadata == {}


def test_every_tenant_scoped_route_declares_the_scope_dependency():
    """Wiring guard: a new handler on these routers that forgets
    `get_tenant_scope` would read across tenants, and no assertion elsewhere
    would notice."""
    scoped_prefixes = (
        "/api/v1/calls",
        "/api/v1/call-logs",
        "/api/v1/contacts",
        "/api/v1/appointments",
        "/api/v1/analytics",
    )
    offenders = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith(scoped_prefixes):
            continue
        dependency_calls = {
            dep.call for dep in route.dependant.dependencies
        } | {
            sub.call
            for dep in route.dependant.dependencies
            for sub in dep.dependencies
        }
        if get_tenant_scope not in dependency_calls:
            offenders.append(f"{sorted(route.methods)} {path}")

    assert not offenders, f"unscoped tenant routes: {offenders}"
