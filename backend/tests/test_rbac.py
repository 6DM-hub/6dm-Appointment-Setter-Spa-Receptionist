"""Requirement 4a: a spa tenant must never reach the 6DM Sales Agent.

The dashboard hides those tabs, but hiding is cosmetic — these tests pin the
server-side contract that makes it safe.
"""
import uuid

import pytest

from app.models import UserRole
from tests.conftest import make_user

TENANT_ID = uuid.uuid4()

#: Every route that belongs to the outbound B2B product.
SALES_ONLY_ENDPOINTS = [
    ("GET", "/api/v1/leads"),
    ("POST", "/api/v1/leads"),
    ("GET", f"/api/v1/leads/{uuid.uuid4()}"),
    ("PATCH", f"/api/v1/leads/{uuid.uuid4()}"),
    ("DELETE", f"/api/v1/leads/{uuid.uuid4()}"),
    ("POST", "/api/v1/telephony/voice/outbound"),
]

SPA_PRINCIPALS = [UserRole.SPA_ADMIN, UserRole.SPA_STAFF]


@pytest.mark.parametrize("method,path", SALES_ONLY_ENDPOINTS)
@pytest.mark.parametrize("role", SPA_PRINCIPALS)
def test_spa_roles_are_forbidden_from_sales_endpoints(
    client, principal, method, path, role
):
    principal.user = make_user(role, tenant_id=TENANT_ID)

    response = client.request(method, path, json={"to_number": "+15550001111"})

    assert response.status_code == 403, (
        f"{role.value} reached {method} {path} "
        f"(got {response.status_code}: {response.text})"
    )
    assert "role" in response.json()["detail"].lower()


@pytest.mark.parametrize("method,path", SALES_ONLY_ENDPOINTS)
def test_super_admin_passes_the_role_gate(client, principal, method, path):
    """The same calls must not be blocked by RBAC for a super admin.

    They can still fail further in (404 for a random lead id, 5xx from the
    mocked database) — what matters is that 403 is not the answer.
    """
    principal.user = make_user(UserRole.SUPER_ADMIN)

    response = client.request(method, path, json={"to_number": "+15550001111"})

    assert response.status_code != 403, f"super_admin blocked on {method} {path}"


def test_spa_user_without_a_tenant_is_refused(client, principal):
    """A spa role whose tenant was deleted has no scope, so it gets nothing
    rather than silently falling back to the platform workspace."""
    principal.user = make_user(UserRole.SPA_ADMIN, tenant_id=None)

    response = client.get("/api/v1/calls")

    assert response.status_code == 403
    assert "not assigned to a spa" in response.json()["detail"]


def test_spa_user_cannot_impersonate_another_tenant(client, principal):
    principal.user = make_user(UserRole.SPA_ADMIN, tenant_id=TENANT_ID)

    response = client.get(
        "/api/v1/calls", headers={"X-Tenant-Id": str(uuid.uuid4())}
    )

    assert response.status_code == 403
    assert "your own spa account" in response.json()["detail"]


def test_spa_staff_cannot_edit_receptionist_settings(client, principal):
    principal.user = make_user(UserRole.SPA_STAFF, tenant_id=TENANT_ID)

    response = client.patch(
        f"/api/v1/spa-accounts/{TENANT_ID}", json={"name": "Renamed"}
    )

    assert response.status_code == 403
    assert "read-only" in response.json()["detail"]


def test_spa_admin_cannot_read_another_spa(client, principal):
    principal.user = make_user(UserRole.SPA_ADMIN, tenant_id=TENANT_ID)

    response = client.get(f"/api/v1/spa-accounts/{uuid.uuid4()}")

    # 404, not 403: whether another tenant exists is not this caller's business.
    assert response.status_code == 404


def test_spa_admin_cannot_provision_accounts(client, principal):
    principal.user = make_user(UserRole.SPA_ADMIN, tenant_id=TENANT_ID)

    response = client.post(
        "/api/v1/spa-accounts",
        json={"name": "My Own New Spa", "twilio_phone_number": "+15550009999"},
    )

    assert response.status_code == 403
