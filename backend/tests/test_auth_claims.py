"""Access tokens must carry role/tenant so the dashboard can pick navigation."""
import uuid

from app.core.security import decode_token
from app.models import UserRole
from tests.conftest import make_user

TENANT_ID = uuid.uuid4()


def _claims(user):
    from app.api.auth import _issue_tokens

    return decode_token(_issue_tokens(user).access_token)


def test_super_admin_token_has_no_tenant():
    claims = _claims(make_user(UserRole.SUPER_ADMIN))

    assert claims["role"] == "super_admin"
    assert claims["tenant_id"] is None


def test_spa_token_pins_the_tenant():
    claims = _claims(make_user(UserRole.SPA_ADMIN, tenant_id=TENANT_ID))

    assert claims["role"] == "spa_admin"
    assert claims["tenant_id"] == str(TENANT_ID)


def test_refresh_tokens_carry_no_role_claim():
    """A refresh token is only ever exchanged; embedding the role would let a
    stale one outlive a demotion."""
    from app.api.auth import _issue_tokens

    claims = decode_token(
        _issue_tokens(make_user(UserRole.SPA_ADMIN, tenant_id=TENANT_ID)).refresh_token
    )

    assert claims["type"] == "refresh"
    assert "role" not in claims
