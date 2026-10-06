"""Row-level scoping for the multi-tenant engine.

Every tenant-owned table (contacts, appointments, call_logs) carries a nullable
`tenant_id`:

  * `tenant_id IS NULL`  -> the 6DM sales workspace, owned by a super admin user
                            via `owner_id` / `user_id`.
  * `tenant_id = <uuid>` -> that spa. Rows here have no owning user, because the
                            AI receptionist creates them with no human involved.

`TenantScope` is the single object that decides which of those a request may
touch. Routers build one via `app.api.deps.get_tenant_scope` and pass it to
`scope_filter()` on read and `scope_columns()` on write, so a missing filter is
a visible omission rather than a silent cross-tenant leak.
"""
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, and_


@dataclass(frozen=True)
class TenantScope:
    """The set of rows a request is allowed to see and create."""

    tenant_id: uuid.UUID | None
    owner_id: uuid.UUID | None

    @property
    def is_sales_workspace(self) -> bool:
        """True when this scope addresses 6DM's own outbound B2B data."""
        return self.tenant_id is None

    @classmethod
    def for_sales_workspace(cls, owner_id: uuid.UUID) -> "TenantScope":
        return cls(tenant_id=None, owner_id=owner_id)

    @classmethod
    def for_tenant(cls, tenant_id: uuid.UUID) -> "TenantScope":
        return cls(tenant_id=tenant_id, owner_id=None)


def _owner_column(model: Any) -> Any:
    """Contacts call it `owner_id`; appointments and call logs use `user_id`."""
    return model.owner_id if hasattr(model, "owner_id") else model.user_id


def scope_filter(scope: TenantScope, model: Any) -> ColumnElement[bool]:
    """WHERE clause restricting `model` rows to `scope`."""
    if scope.tenant_id is not None:
        return model.tenant_id == scope.tenant_id
    return and_(model.tenant_id.is_(None), _owner_column(model) == scope.owner_id)


def call_log_scope_filter(scope: TenantScope, model: Any) -> ColumnElement[bool]:
    """Scope call logs and hide sales calls from every spa tenant."""
    clause = scope_filter(scope, model)
    if scope.tenant_id is not None:
        clause = and_(clause, model.direction == "inbound")
    return clause


def scope_columns(scope: TenantScope, model: Any) -> dict[str, Any]:
    """INSERT kwargs that place a new row inside `scope`.

    Exactly one of the two ownership columns is set, matching the
    `num_nonnulls(...) = 1` check constraints on contacts and appointments.
    """
    if scope.tenant_id is not None:
        return {"tenant_id": scope.tenant_id}
    return {_owner_column(model).key: scope.owner_id}


def owns(scope: TenantScope, row: Any) -> bool:
    """Whether an already-loaded row belongs to `scope`.

    Used by the get/patch/delete-by-id handlers, which fetch by primary key and
    then have to decide between returning the row and 404-ing.
    """
    if scope.tenant_id is not None:
        return row.tenant_id == scope.tenant_id
    row_owner = getattr(row, "owner_id", None) or getattr(row, "user_id", None)
    return row.tenant_id is None and row_owner == scope.owner_id
