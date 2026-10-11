import copy
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.api.v1 import services as services_api
from app.api.v1 import spa_accounts as spa_accounts_api
from app.core.database import get_db
from app.core.tenancy import TenantScope
from app.main import app
from app.models import BookingProvider, Service, ServiceCategory, SpaAccount, User, UserRole
from app.schemas.service import SquareCatalogApply, SquareCatalogPreview
from app.schemas.spa_account import SpaAccountCreate
from app.services import square_catalog_sync as sync
from app.services.booking_adapters.providers import square as square_provider
from app.services.booking_config import encrypt_config


def _service(**overrides):
    values = {
        "id": uuid.uuid4(),
        "spa_id": uuid.uuid4(),
        "category_id": uuid.uuid4(),
        "name": "European Facial",
        "description": "Owner wording",
        "price": "100",
        "duration_minutes": 60,
        "is_active": True,
        "square_item_id": None,
        "square_variation_id": None,
        "square_variation_version": None,
        "voice_metadata": {},
    }
    values.update(overrides)
    return Service(**values)


def _square_objects():
    return [
        {
            "type": "ITEM",
            "id": "ITEM-FACIAL",
            "item_data": {
                "name": "European Facial",
                "product_type": "APPOINTMENTS_SERVICE",
                "description": "Square wording",
                "variations": [
                    {
                        "type": "ITEM_VARIATION",
                        "id": "VAR-FACIAL-60",
                        "version": 1_796_000_000_123,
                        "item_variation_data": {
                            "name": "60 Minutes",
                            "available_for_booking": True,
                            "service_duration": 3_600_000,
                            "price_money": {"amount": 12500, "currency": "USD"},
                        },
                    },
                    {
                        "type": "ITEM_VARIATION",
                        "id": "NOT-BOOKABLE",
                        "version": 1,
                        "item_variation_data": {
                            "name": "Internal",
                            "available_for_booking": False,
                            "service_duration": 3_600_000,
                        },
                    },
                ],
            },
        },
        {
            "type": "ITEM",
            "id": "RETAIL",
            "item_data": {
                "name": "Face cream",
                "product_type": "REGULAR",
                "variations": [],
            },
        },
    ]


def _catalog():
    return sync.normalize_square_catalog(_square_objects())


def _mock_catalog_apply_state(
    monkeypatch,
    *,
    spa,
    services,
    catalog,
    fetched_provider="provider-a",
    locked_provider=None,
    location_name="Your Day Spa",
):
    monkeypatch.setattr(
        services_api,
        "_catalog_state",
        AsyncMock(
            return_value=(spa, services, catalog, location_name, fetched_provider)
        ),
    )
    monkeypatch.setattr(
        services_api,
        "_lock_catalog_apply_state",
        AsyncMock(
            return_value=(
                spa,
                services,
                fetched_provider if locked_provider is None else locked_provider,
            )
        ),
    )


def test_normalize_catalog_keeps_only_bookable_appointment_variations_and_int64_version():
    rows = _catalog()

    assert len(rows) == 1
    assert rows[0]["square_item_id"] == "ITEM-FACIAL"
    assert rows[0]["square_variation_id"] == "VAR-FACIAL-60"
    assert rows[0]["square_version"] == 1_796_000_000_123
    assert rows[0]["duration_minutes"] == 60
    assert rows[0]["price"] == "125"
    assert rows[0]["price_amount"] == 12500
    assert rows[0]["price_currency"] == "USD"
    assert rows[0]["category_name"] == "Facial"


def test_normalize_catalog_requires_explicit_square_appointment_object_types():
    valid = _square_objects()[0]
    invalid = []
    for mutation in ("missing_item_type", "missing_product_type", "missing_variation_type"):
        candidate = copy.deepcopy(valid)
        candidate["id"] = mutation
        candidate["item_data"]["variations"][0]["id"] = f"VAR-{mutation}"
        if mutation == "missing_item_type":
            candidate.pop("type")
        elif mutation == "missing_product_type":
            candidate["item_data"].pop("product_type")
        else:
            candidate["item_data"]["variations"][0].pop("type")
        invalid.append(candidate)

    rows = sync.normalize_square_catalog([*invalid, valid])

    assert [row["square_variation_id"] for row in rows] == ["VAR-FACIAL-60"]


def test_preview_matches_item_name_and_duration_without_creating_a_duplicate():
    service = _service()
    items, summary = sync.build_preview(_catalog(), [service])

    assert summary == {"additions": 0, "updates": 1, "conflicts": 0, "unchanged": 0}
    assert items[0]["action"] == "update"
    assert items[0]["existing_service_id"] == str(service.id)
    assert {change["field"] for change in items[0]["changes"]} >= {
        "square_item_id",
        "square_variation_id",
        "square_version",
    }
    # Response contract is intentionally narrower than internal provider data.
    public = SquareCatalogPreview.model_validate(
        {
            "preview_token": "x" * 20,
            "connected": True,
            "generated_at": "2026-10-11T00:00:00Z",
            "summary": summary,
            "items": items,
        }
    ).model_dump()
    assert set(public["items"][0]) == {
        "square_item_id",
        "square_variation_id",
        "square_version",
        "name",
        "category_name",
        "duration_minutes",
        "price",
        "action",
        "existing_service_id",
        "changes",
        "conflict_reason",
    }
    assert public["location_name"] is None


def test_preview_discloses_square_description_only_when_apply_would_write_it():
    empty_description = _service(description=None)
    owner_description = _service(description="Keep the owner's spa wording")

    empty_item = sync.build_preview(_catalog(), [empty_description])[0][0]
    owner_item = sync.build_preview(_catalog(), [owner_description])[0][0]

    assert {
        "field": "description",
        "current": None,
        "square": "Square wording",
    } in empty_item["changes"]
    assert all(change["field"] != "description" for change in owner_item["changes"])


def test_duplicate_local_square_mapping_is_a_conflict_not_an_automatic_update():
    first = _service(square_variation_id="VAR-FACIAL-60")
    second = _service(square_variation_id="VAR-FACIAL-60")

    items, summary = sync.build_preview(_catalog(), [first, second])

    assert summary["conflicts"] == 1
    assert items[0]["action"] == "conflict"
    assert items[0]["existing_service_id"] is None
    assert items[0]["conflict_reason"] == "multiple_local_services_match"


def test_preview_token_rejects_tampering_expiry_and_changed_catalog_or_local_state():
    spa_id = uuid.uuid4()
    token = sync.make_preview_token(
        spa_id, "catalog-a", "local-a", "provider-a", issued_at=1000
    )
    sync.verify_preview_token(
        token, spa_id, "catalog-a", "local-a", "provider-a", now=1001
    )

    with pytest.raises(sync.StaleCatalogPreview, match="invalid"):
        sync.verify_preview_token(
            token + "x", spa_id, "catalog-a", "local-a", "provider-a", now=1001
        )
    with pytest.raises(sync.StaleCatalogPreview, match="expired"):
        sync.verify_preview_token(
            token,
            spa_id,
            "catalog-a",
            "local-a",
            "provider-a",
            now=1000 + sync.PREVIEW_TTL_SECONDS + 1,
        )
    with pytest.raises(sync.StaleCatalogPreview, match="Square catalog changed"):
        sync.verify_preview_token(
            token, spa_id, "catalog-b", "local-a", "provider-a", now=1001
        )
    with pytest.raises(sync.StaleCatalogPreview, match="Cara services changed"):
        sync.verify_preview_token(
            token, spa_id, "catalog-a", "local-b", "provider-a", now=1001
        )
    with pytest.raises(sync.StaleCatalogPreview, match="Square connection changed"):
        sync.verify_preview_token(
            token, spa_id, "catalog-a", "local-a", "provider-b", now=1001
        )
    with pytest.raises(sync.StaleCatalogPreview, match="another business"):
        sync.verify_preview_token(
            token, uuid.uuid4(), "catalog-a", "local-a", "provider-a", now=1001
        )


def test_provider_identity_fingerprint_changes_with_location_or_environment():
    production = sync.provider_identity_fingerprint(
        {"location_id": "LOCATION-A", "environment": "production", "access_token": "secret"}
    )
    other_location = sync.provider_identity_fingerprint(
        {"location_id": "LOCATION-B", "environment": "production", "access_token": "secret"}
    )
    sandbox = sync.provider_identity_fingerprint(
        {"location_id": "LOCATION-A", "environment": "sandbox", "access_token": "secret"}
    )

    assert len(production) == 64
    assert len({production, other_location, sandbox}) == 3
    assert "LOCATION-A" not in production
    assert "secret" not in production


def test_apply_schema_capacity_matches_bounded_catalog_fetch():
    selections = [
        {"square_variation_id": f"VAR-{index}", "action": "skip"}
        for index in range(2_500)
    ]

    payload = SquareCatalogApply.model_validate(
        {"preview_token": "x" * 20, "selections": selections}
    )

    assert len(payload.selections) == 2_500
    with pytest.raises(ValidationError):
        SquareCatalogApply.model_validate(
            {
                "preview_token": "x" * 20,
                "selections": [
                    *selections,
                    {"square_variation_id": "VAR-2500", "action": "skip"},
                ],
            }
        )


@pytest.mark.asyncio
async def test_catalog_fetch_is_read_only_and_paginates(monkeypatch):
    calls = []

    class FakeSquare:
        def __init__(self, _spa_name, _config):
            self.location_id = "LOCATION"

        async def describe_location(self):
            return {"id": "LOCATION", "name": "Your Day Spa"}

        async def _request(self, method, path, *, json):
            calls.append((method, path, json))
            if len(calls) == 1:
                return {"items": _square_objects(), "cursor": "NEXT"}
            return {"items": []}

    monkeypatch.setattr(sync, "SquareAdapter", FakeSquare)
    spa = SpaAccount(id=uuid.uuid4(), name="Spa")

    rows, location_name = await sync.fetch_square_catalog(
        spa, {"access_token": "never-logged", "location_id": "LOCATION"}
    )

    assert len(rows) == 1
    assert location_name == "Your Day Spa"
    assert [method for method, _, _ in calls] == ["POST", "POST"]
    assert all(path == "/v2/catalog/search-catalog-items" for _, path, _ in calls)
    assert all(payload["limit"] == 100 for _, _, payload in calls)
    assert calls[1][2]["cursor"] == "NEXT"


@pytest.mark.asyncio
async def test_apply_adds_only_explicit_selection_and_requires_local_category(monkeypatch):
    spa_id = uuid.uuid4()
    category = ServiceCategory(id=uuid.uuid4(), spa_id=spa_id, name="Facial", is_active=True)
    spa = SpaAccount(id=spa_id, name="Spa", booking_provider=BookingProvider.SQUARE)
    catalog = _catalog()
    token = sync.make_preview_token(
        spa_id, sync.catalog_fingerprint(catalog), sync.local_fingerprint([]), "provider-a"
    )
    _mock_catalog_apply_state(
        monkeypatch, spa=spa, services=[], catalog=catalog
    )
    legacy_sync = AsyncMock()
    monkeypatch.setattr(services_api, "_sync_legacy_services", legacy_sync)

    scalar_result = MagicMock()
    scalar_result.scalars.return_value = [category]
    db = MagicMock()
    db.execute = AsyncMock(return_value=scalar_result)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    result = await services_api.apply_square_catalog(
        SquareCatalogApply.model_validate(
            {
                "preview_token": token,
                "selections": [
                    {
                        "square_variation_id": "VAR-FACIAL-60",
                        "action": "add",
                        "category_id": str(category.id),
                    }
                ],
            }
        ),
        db=db,
        scope=TenantScope.for_tenant(spa_id),
    )

    assert result.model_dump() == {"created": 1, "updated": 0, "skipped": 0, "conflicts": 0}
    added = db.add.call_args.args[0]
    assert added.spa_id == spa_id
    assert added.square_variation_id == "VAR-FACIAL-60"
    assert added.square_variation_version == 1_796_000_000_123
    assert added.category_id == category.id
    category_statement = db.execute.await_args.args[0]
    assert "FOR UPDATE" in str(
        category_statement.compile(dialect=postgresql.dialect())
    )
    assert category_statement.get_execution_options()["populate_existing"] is True
    legacy_sync.assert_awaited_once_with(db, spa_id, legacy_overrides={})
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_reports_concurrent_category_deletion_as_category_conflict(monkeypatch):
    class ForeignKeyFailure(Exception):
        sqlstate = "23503"

    spa_id = uuid.uuid4()
    category = ServiceCategory(
        id=uuid.uuid4(), spa_id=spa_id, name="Facial", is_active=True
    )
    spa = SpaAccount(id=spa_id, name="Spa", booking_provider=BookingProvider.SQUARE)
    catalog = _catalog()
    token = sync.make_preview_token(
        spa_id,
        sync.catalog_fingerprint(catalog),
        sync.local_fingerprint([]),
        "provider-a",
    )
    _mock_catalog_apply_state(monkeypatch, spa=spa, services=[], catalog=catalog)
    result = MagicMock()
    result.scalars.return_value = [category]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.flush = AsyncMock(
        side_effect=IntegrityError("INSERT INTO services", {}, ForeignKeyFailure())
    )
    db.rollback = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await services_api.apply_square_catalog(
            SquareCatalogApply.model_validate(
                {
                    "preview_token": token,
                    "selections": [
                        {
                            "square_variation_id": "VAR-FACIAL-60",
                            "action": "add",
                            "category_id": str(category.id),
                        }
                    ],
                }
            ),
            db=db,
            scope=TenantScope.for_tenant(spa_id),
        )

    assert exc.value.status_code == 409
    assert "category changed or was deleted" in exc.value.detail
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_refetch_guard_rejects_changed_square_catalog(monkeypatch):
    spa_id = uuid.uuid4()
    spa = SpaAccount(id=spa_id, name="Spa", booking_provider=BookingProvider.SQUARE)
    original = _catalog()
    changed = [dict(original[0], price="130", price_amount=13000)]
    token = sync.make_preview_token(
        spa_id, sync.catalog_fingerprint(original), sync.local_fingerprint([]), "provider-a"
    )
    _mock_catalog_apply_state(
        monkeypatch, spa=spa, services=[], catalog=changed
    )
    db = MagicMock()

    with pytest.raises(HTTPException) as exc:
        await services_api.apply_square_catalog(
            SquareCatalogApply(preview_token=token, selections=[]),
            db=db,
            scope=TenantScope.for_tenant(spa_id),
        )

    assert exc.value.status_code == 409
    assert "Square catalog changed" in exc.value.detail


@pytest.mark.asyncio
async def test_apply_refetch_guard_rejects_changed_square_connection(monkeypatch):
    spa_id = uuid.uuid4()
    spa = SpaAccount(id=spa_id, name="Spa", booking_provider=BookingProvider.SQUARE)
    catalog = _catalog()
    token = sync.make_preview_token(
        spa_id,
        sync.catalog_fingerprint(catalog),
        sync.local_fingerprint([]),
        "provider-location-a",
    )
    _mock_catalog_apply_state(
        monkeypatch,
        spa=spa,
        services=[],
        catalog=catalog,
        fetched_provider="provider-location-b",
        location_name="Other Spa",
    )
    db = MagicMock()

    with pytest.raises(HTTPException) as exc:
        await services_api.apply_square_catalog(
            SquareCatalogApply(preview_token=token, selections=[]),
            db=db,
            scope=TenantScope.for_tenant(spa_id),
        )

    assert exc.value.status_code == 409
    assert "Square connection changed" in exc.value.detail


@pytest.mark.asyncio
async def test_apply_rechecks_locked_local_state_after_live_square_fetch(monkeypatch):
    spa_id = uuid.uuid4()
    service_id = uuid.uuid4()
    category_id = uuid.uuid4()
    preview_service = _service(
        id=service_id,
        spa_id=spa_id,
        category_id=category_id,
        price="100",
    )
    concurrently_edited = _service(
        id=service_id,
        spa_id=spa_id,
        category_id=category_id,
        price="115",
    )
    spa = SpaAccount(id=spa_id, name="Spa", booking_provider=BookingProvider.SQUARE)
    catalog = _catalog()
    token = sync.make_preview_token(
        spa_id,
        sync.catalog_fingerprint(catalog),
        sync.local_fingerprint([preview_service]),
        "provider-a",
    )
    monkeypatch.setattr(
        services_api,
        "_catalog_state",
        AsyncMock(
            return_value=(
                spa,
                [preview_service],
                catalog,
                "Your Day Spa",
                "provider-a",
            )
        ),
    )
    lock = AsyncMock(return_value=(spa, [concurrently_edited], "provider-a"))
    monkeypatch.setattr(services_api, "_lock_catalog_apply_state", lock)
    db = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await services_api.apply_square_catalog(
            SquareCatalogApply(preview_token=token, selections=[]),
            db=db,
            scope=TenantScope.for_tenant(spa_id),
        )

    assert exc.value.status_code == 409
    assert "Cara services changed" in exc.value.detail
    lock.assert_awaited_once_with(db, spa_id)
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_apply_lock_uses_for_update_and_refreshes_identity_map():
    spa_id = uuid.uuid4()
    spa = SpaAccount(
        id=spa_id,
        name="Spa",
        booking_provider=BookingProvider.SQUARE,
        booking_config=encrypt_config(
            {
                "access_token": "secret",
                "location_id": "LOCATION",
                "environment": "production",
            }
        ),
    )
    service = _service(spa_id=spa_id)
    spa_result = MagicMock()
    spa_result.scalar_one_or_none.return_value = spa
    service_result = MagicMock()
    service_result.scalars.return_value = [service]
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[spa_result, service_result])

    locked_spa, locked_services, provider_hash = (
        await services_api._lock_catalog_apply_state(db, spa_id)
    )

    assert locked_spa is spa
    assert locked_services == [service]
    assert provider_hash == sync.provider_identity_fingerprint(
        {
            "access_token": "secret",
            "location_id": "LOCATION",
            "environment": "production",
        }
    )
    statements = [call.args[0] for call in db.execute.await_args_list]
    assert len(statements) == 2
    for statement in statements:
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE" in sql
        assert statement.get_execution_options()["populate_existing"] is True


@pytest.mark.asyncio
async def test_update_preserves_category_and_rejects_hidden_category_change(monkeypatch):
    spa_id = uuid.uuid4()
    original_category_id = uuid.uuid4()
    other_category_id = uuid.uuid4()
    service = _service(
        spa_id=spa_id,
        category_id=original_category_id,
        description=None,
        square_variation_id="VAR-FACIAL-60",
        square_variation_version=1,
    )
    spa = SpaAccount(id=spa_id, name="Spa", booking_provider=BookingProvider.SQUARE)
    catalog = _catalog()
    token = sync.make_preview_token(
        spa_id,
        sync.catalog_fingerprint(catalog),
        sync.local_fingerprint([service]),
        "provider-a",
    )
    _mock_catalog_apply_state(
        monkeypatch, spa=spa, services=[service], catalog=catalog
    )
    monkeypatch.setattr(services_api, "_sync_legacy_services", AsyncMock())
    category = ServiceCategory(id=other_category_id, spa_id=spa_id, name="Massage", is_active=True)
    scalar_result = MagicMock()
    scalar_result.scalars.return_value = [category]
    db = MagicMock()
    db.execute = AsyncMock(return_value=scalar_result)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await services_api.apply_square_catalog(
            SquareCatalogApply.model_validate(
                {
                    "preview_token": token,
                    "selections": [
                        {
                            "square_variation_id": "VAR-FACIAL-60",
                            "action": "update",
                            "category_id": str(other_category_id),
                        }
                    ],
                }
            ),
            db=db,
            scope=TenantScope.for_tenant(spa_id),
        )

    assert exc.value.status_code == 400
    assert service.category_id == original_category_id
    assert service.description is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selections", "expected_skipped"),
    [
        ([], 0),
        ([{"square_variation_id": "VAR-FACIAL-60", "action": "skip"}], 1),
    ],
)
async def test_empty_or_all_skip_apply_does_not_write(
    monkeypatch, selections, expected_skipped
):
    spa_id = uuid.uuid4()
    spa = SpaAccount(id=spa_id, name="Spa", booking_provider=BookingProvider.SQUARE)
    spa.services = []
    catalog = _catalog()
    token = sync.make_preview_token(
        spa_id,
        sync.catalog_fingerprint(catalog),
        sync.local_fingerprint([]),
        "provider-a",
    )
    _mock_catalog_apply_state(
        monkeypatch, spa=spa, services=[], catalog=catalog
    )
    db = MagicMock()
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    result = await services_api.apply_square_catalog(
        SquareCatalogApply.model_validate(
            {"preview_token": token, "selections": selections}
        ),
        db=db,
        scope=TenantScope.for_tenant(spa_id),
    )

    assert result.model_dump() == {
        "created": 0,
        "updated": 0,
        "skipped": expected_skipped,
        "conflicts": 0,
    }
    db.execute.assert_not_awaited()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_add_rejects_category_from_another_tenant_without_writing(monkeypatch):
    spa_id = uuid.uuid4()
    other_category_id = uuid.uuid4()
    spa = SpaAccount(id=spa_id, name="Spa", booking_provider=BookingProvider.SQUARE)
    spa.services = []
    catalog = _catalog()
    token = sync.make_preview_token(
        spa_id,
        sync.catalog_fingerprint(catalog),
        sync.local_fingerprint([]),
        "provider-a",
    )
    _mock_catalog_apply_state(
        monkeypatch, spa=spa, services=[], catalog=catalog
    )
    result = MagicMock()
    result.scalars.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await services_api.apply_square_catalog(
            SquareCatalogApply.model_validate(
                {
                    "preview_token": token,
                    "selections": [
                        {
                            "square_variation_id": "VAR-FACIAL-60",
                            "action": "add",
                            "category_id": str(other_category_id),
                        }
                    ],
                }
            ),
            db=db,
            scope=TenantScope.for_tenant(spa_id),
        )

    assert exc.value.status_code == 400
    assert "belonging to this spa" in exc.value.detail
    db.add.assert_not_called()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_square_link_preserves_legacy_metadata_when_approved_name_changes(monkeypatch):
    spa_id = uuid.uuid4()
    category_id = uuid.uuid4()
    service = _service(spa_id=spa_id, category_id=category_id)
    spa = SpaAccount(id=spa_id, name="Spa", booking_provider=BookingProvider.SQUARE)
    spa.services = [
        {
            "name": "European Facial",
            "duration_minutes": 60,
            "consultation_kind": "facial",
            "consultation_tags": ["dryness"],
            "aliases": ["Euro facial"],
            "preparation_buffer_minutes": 5,
            "transition_buffer_minutes": 10,
            "cleanup_buffer_minutes": 15,
            "is_add_on": False,
        }
    ]
    catalog = _catalog()
    token = sync.make_preview_token(
        spa_id,
        sync.catalog_fingerprint(catalog),
        sync.local_fingerprint([service]),
        "provider-a",
    )
    _mock_catalog_apply_state(
        monkeypatch, spa=spa, services=[service], catalog=catalog
    )
    row_result = MagicMock()
    row_result.all.return_value = [(service, "Facial")]
    db = MagicMock()
    db.get = AsyncMock(return_value=spa)
    db.execute = AsyncMock(return_value=row_result)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    result = await services_api.apply_square_catalog(
        SquareCatalogApply.model_validate(
            {
                "preview_token": token,
                "selections": [
                    {"square_variation_id": "VAR-FACIAL-60", "action": "update"}
                ],
            }
        ),
        db=db,
        scope=TenantScope.for_tenant(spa_id),
    )

    assert result.updated == 1
    assert service.name == "European Facial (60 min)"
    assert spa.services[0]["consultation_kind"] == "facial"
    assert spa.services[0]["consultation_tags"] == ["dryness"]
    assert spa.services[0]["aliases"] == ["Euro facial"]
    assert spa.services[0]["preparation_buffer_minutes"] == 5
    assert spa.services[0]["transition_buffer_minutes"] == 10
    assert spa.services[0]["cleanup_buffer_minutes"] == 15
    assert spa.services[0]["square_variation_id"] == "VAR-FACIAL-60"


@pytest.mark.asyncio
async def test_conflict_row_cannot_be_applied_as_update(monkeypatch):
    spa_id = uuid.uuid4()
    first = _service(spa_id=spa_id, square_variation_id="VAR-FACIAL-60")
    second = _service(spa_id=spa_id, square_variation_id="VAR-FACIAL-60")
    catalog = _catalog()
    spa = SpaAccount(id=spa_id, name="Spa", booking_provider=BookingProvider.SQUARE)
    token = sync.make_preview_token(
        spa_id,
        sync.catalog_fingerprint(catalog),
        sync.local_fingerprint([first, second]),
        "provider-a",
    )
    _mock_catalog_apply_state(
        monkeypatch, spa=spa, services=[first, second], catalog=catalog
    )
    db = MagicMock()

    with pytest.raises(HTTPException) as exc:
        await services_api.apply_square_catalog(
            SquareCatalogApply.model_validate(
                {
                    "preview_token": token,
                    "selections": [
                        {"square_variation_id": "VAR-FACIAL-60", "action": "update"}
                    ],
                }
            ),
            db=db,
            scope=TenantScope.for_tenant(spa_id),
        )

    assert exc.value.status_code == 409
    assert "Resolve the local conflict" in exc.value.detail


@pytest.mark.asyncio
async def test_square_catalog_read_retries_one_transient_failure(monkeypatch):
    class Response:
        def __init__(self, status_code, data):
            self.status_code = status_code
            self._data = data
            self.is_error = status_code >= 400
            self.content = b"{}"

        def json(self):
            return self._data

    responses = [Response(503, {"errors": [{"code": "TEMPORARY_ERROR"}]}), Response(200, {"items": []})]
    request = AsyncMock(side_effect=responses)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, *args, **kwargs):
            return await request(*args, **kwargs)

    monkeypatch.setattr(square_provider.httpx, "AsyncClient", Client)
    monkeypatch.setattr(square_provider.asyncio, "sleep", AsyncMock())
    adapter = square_provider.SquareAdapter(
        "Spa", {"access_token": "secret", "location_id": "LOCATION"}
    )

    result = await adapter._request(
        "POST",
        "/v2/catalog/search-catalog-items",
        json={"product_types": ["APPOINTMENTS_SERVICE"]},
    )

    assert result == {"items": []}
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_legacy_menu_sync_preserves_consultation_metadata_and_square_mapping():
    spa_id = uuid.uuid4()
    category_id = uuid.uuid4()
    spa = SpaAccount(id=spa_id, name="Spa")
    spa.services = [
        {
            "name": "European Facial",
            "duration_minutes": 60,
            "consultation_kind": "facial",
            "aliases": ["European"],
            "transition_buffer_minutes": 10,
        }
    ]
    service = _service(
        spa_id=spa_id,
        category_id=category_id,
        name="European Facial (60 min)",
        square_item_id="ITEM-FACIAL",
        square_variation_id="VAR-FACIAL-60",
        square_variation_version=1_796_000_000_123,
    )
    result = MagicMock()
    result.all.return_value = [(service, "Facial")]
    db = MagicMock()
    db.get = AsyncMock(return_value=spa)
    db.execute = AsyncMock(return_value=result)

    await services_api._sync_legacy_services(db, spa_id)

    assert spa.services == [
        {
            "name": "European Facial (60 min)",
            "duration_minutes": 60,
            "consultation_kind": "facial",
            "aliases": ["European"],
            "transition_buffer_minutes": 10,
            "price": "100",
            "description": "Owner wording",
            "category": "Facial",
            "square_item_id": "ITEM-FACIAL",
            "square_variation_id": "VAR-FACIAL-60",
            "square_variation_version": 1_796_000_000_123,
        }
    ]
    assert service.voice_metadata == {
        "consultation_kind": "facial",
        "aliases": ["European"],
        "transition_buffer_minutes": 10,
    }


@pytest.mark.asyncio
async def test_deactivate_then_reactivate_preserves_voice_metadata():
    spa_id = uuid.uuid4()
    service = _service(spa_id=spa_id, is_active=False)
    spa = SpaAccount(id=spa_id, name="Spa")
    spa.services = [
        {
            "name": "European Facial",
            "duration_minutes": 60,
            "consultation_kind": "facial",
            "consultation_tags": ["dryness"],
            "aliases": ["European"],
            "preparation_buffer_minutes": 5,
            "transition_buffer_minutes": 10,
            "cleanup_buffer_minutes": 15,
        }
    ]
    result = MagicMock()
    result.all.return_value = [(service, "Facial")]
    db = MagicMock()
    db.get = AsyncMock(return_value=spa)
    db.execute = AsyncMock(return_value=result)

    await services_api._sync_legacy_services(db, spa_id)

    assert spa.services == []
    assert service.voice_metadata["consultation_kind"] == "facial"
    assert service.voice_metadata["consultation_tags"] == ["dryness"]
    assert service.voice_metadata["aliases"] == ["European"]
    assert service.voice_metadata["preparation_buffer_minutes"] == 5
    assert service.voice_metadata["transition_buffer_minutes"] == 10
    assert service.voice_metadata["cleanup_buffer_minutes"] == 15

    service.is_active = True
    await services_api._sync_legacy_services(db, spa_id)

    assert len(spa.services) == 1
    assert spa.services[0]["consultation_kind"] == "facial"
    assert spa.services[0]["consultation_tags"] == ["dryness"]
    assert spa.services[0]["aliases"] == ["European"]
    assert spa.services[0]["preparation_buffer_minutes"] == 5
    assert spa.services[0]["transition_buffer_minutes"] == 10
    assert spa.services[0]["cleanup_buffer_minutes"] == 15


def _test_user(role: UserRole, tenant_id: uuid.UUID) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{role.value}@example.test",
        hashed_password="test",
        role=role,
        tenant_id=tenant_id,
        is_active=True,
    )
    return user


def test_service_and_category_mutations_reject_spa_staff(client, principal):
    tenant_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    category_id = uuid.uuid4()
    principal.user = _test_user(UserRole.SPA_STAFF, tenant_id)
    service_body = {
        "name": "Facial",
        "category_id": str(category_id),
        "duration_minutes": 60,
        "is_active": True,
    }
    requests = [
        ("post", "/api/v1/services/categories", {"name": "Facial"}),
        ("patch", f"/api/v1/services/categories/{resource_id}", {"name": "Facial"}),
        ("delete", f"/api/v1/services/categories/{resource_id}", None),
        ("post", "/api/v1/services", service_body),
        ("patch", f"/api/v1/services/{resource_id}", service_body),
        ("delete", f"/api/v1/services/{resource_id}", None),
        ("post", "/api/v1/services/bulk-delete", {"delete_all": True}),
    ]

    for method, path, body in requests:
        response = client.request(method, path, json=body)
        assert response.status_code == 403, (method, path, response.text)


def test_spa_admin_can_mutate_services(client, principal):
    tenant_id = uuid.uuid4()
    principal.user = _test_user(UserRole.SPA_ADMIN, tenant_id)
    spa = SpaAccount(id=tenant_id, name="Spa")
    spa.services = []
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.all.return_value = []
    db = MagicMock()
    db.get = AsyncMock(return_value=spa)
    db.execute = AsyncMock(return_value=result)
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    async def _db():
        yield db

    app.dependency_overrides[get_db] = _db

    response = client.post("/api/v1/services/bulk-delete", json={"delete_all": True})

    assert response.status_code == 200
    assert response.json() == {"deleted": 0}
    db.commit.assert_awaited_once()


def test_spa_staff_service_lists_are_read_only(client, principal):
    tenant_id = uuid.uuid4()
    principal.user = _test_user(UserRole.SPA_STAFF, tenant_id)
    categories_result = MagicMock()
    categories_result.all.return_value = []
    services_result = MagicMock()
    services_result.scalars.return_value.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[categories_result, services_result])
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    async def _db():
        yield db

    app.dependency_overrides[get_db] = _db

    categories = client.get("/api/v1/services/categories")
    services = client.get("/api/v1/services")

    assert categories.status_code == 200
    assert categories.json() == []
    assert services.status_code == 200
    assert services.json() == []
    db.add.assert_not_called()
    db.add_all.assert_not_called()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_category_locks_before_checking_for_services():
    spa_id = uuid.uuid4()
    category = ServiceCategory(
        id=uuid.uuid4(), spa_id=spa_id, name="Facial", is_active=True
    )
    category_result = MagicMock()
    category_result.scalar_one_or_none.return_value = category
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[category_result, count_result])
    db.delete = AsyncMock()
    db.commit = AsyncMock()

    await services_api.delete_category(
        category.id,
        db=db,
        scope=TenantScope.for_tenant(spa_id),
    )

    category_statement = db.execute.await_args_list[0].args[0]
    assert "FOR UPDATE" in str(
        category_statement.compile(dialect=postgresql.dialect())
    )
    assert category_statement.get_execution_options()["populate_existing"] is True
    db.delete.assert_awaited_once_with(category)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_spa_provisions_default_categories_in_manager_write(monkeypatch):
    spa_id = uuid.uuid4()
    db = MagicMock()
    db.add = MagicMock()
    db.add_all = MagicMock()

    async def _flush():
        db.add.call_args.args[0].id = spa_id

    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    monkeypatch.setattr(spa_accounts_api, "_assert_number_free", AsyncMock())
    monkeypatch.setattr(spa_accounts_api, "_to_read", lambda spa: spa)

    spa = await spa_accounts_api.create_spa_account(
        SpaAccountCreate(name="New Spa"), db=db
    )

    assert spa.id == spa_id
    categories = db.add_all.call_args.args[0]
    assert {category.name for category in categories} == set(
        spa_accounts_api.DEFAULT_SERVICE_CATEGORIES
    )
    assert all(category.spa_id == spa_id for category in categories)
    db.flush.assert_awaited_once()
    db.commit.assert_awaited_once()

