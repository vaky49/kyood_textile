from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.models import Catalogue, CatalogueItem, Supplier
from app.infrastructure.db.base import Base
from app.infrastructure.db.session import get_session
from app.main import app
from app.settings import Settings, get_settings


@pytest.mark.asyncio
async def test_catalogue_items_keep_provenance_and_staleness_in_search() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[object]:
        async with session_factory() as session:
            yield session

    async with session_factory() as session:
        supplier = Supplier(
            business_name="Surat Cotton Works",
            city="Surat",
            state="Gujarat",
            is_verified=True,
        )
        session.add(supplier)
        await session.commit()
        supplier_id = str(supplier.id)

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(operator_api_token="test-token")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            headers = {"X-Operator-Token": "test-token"}
            verified_at = datetime.now(UTC) - timedelta(days=45)
            catalogue_response = await client.post(
                "/api/catalogues",
                headers=headers,
                json={
                    "supplier_id": supplier_id,
                    "source_type": "manual",
                    "last_verified_at": verified_at.isoformat(),
                },
            )
            assert catalogue_response.status_code == 201
            catalogue_id = catalogue_response.json()["id"]

            item_response = await client.post(
                f"/api/catalogues/{catalogue_id}/items",
                headers=headers,
                json={
                    "title": "Printed cotton suit set",
                    "category": "ladies_suit",
                    "fabric": "cotton",
                    "price": "550.00",
                    "currency": "INR",
                    "moq": 50,
                    "last_verified_at": verified_at.isoformat(),
                },
            )
            assert item_response.status_code == 201
            item = item_response.json()
            assert item["fact_provenance"]["price"]["source"] == "supplier_catalogue"
            assert item["fact_provenance"]["price"]["source_id"] == catalogue_id

            search = await client.post(
                "/api/catalogues/search",
                headers=headers,
                json={
                    "category": "ladies_suit",
                    "fabric": "cotton",
                    "quantity": 100,
                    "target_price_min": "500",
                    "target_price_max": "600",
                    "currency": "INR",
                    "delivery_city": "Jaipur",
                },
            )
            assert search.status_code == 200
            result = search.json()[0]
            assert result["catalogue_item_id"] == item["id"]
            assert result["price"] == "550.00"
            assert result["available_quantity"] is None
            assert result["catalogue_age_days"] >= 44
            assert "Catalogue information is stale" in result["reasons"]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalogue_upload_imports_rows_for_review_and_verify_activates_them(tmp_path) -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[object]:
        async with session_factory() as session:
            yield session

    async with session_factory() as session:
        supplier = Supplier(
            business_name="Ahmedabad Suit Studio",
            city="Ahmedabad",
            state="Gujarat",
        )
        session.add(supplier)
        await session.commit()
        supplier_id = str(supplier.id)

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(
        operator_api_token="test-token",
        media_storage_dir=str(tmp_path),
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            headers = {"X-Operator-Token": "test-token"}
            created = await client.post(
                "/api/catalogues",
                headers=headers,
                json={"supplier_id": supplier_id, "source_type": "manual"},
            )
            assert created.status_code == 201
            catalogue_id = created.json()["id"]

            csv_content = (
                b"title,category,fabric,price,currency,moq,available_quantity\n"
                b"Cotton suit,ladies_suit,cotton,550,INR,50,120\n"
                b"Unknown item,other,,500,INR,10,\n"
            )
            uploaded = await client.post(
                f"/api/catalogues/{catalogue_id}/upload",
                headers=headers,
                files={"file": ("catalogue.csv", csv_content, "text/csv")},
            )
            assert uploaded.status_code == 202
            assert uploaded.json()["items_created"] == 1
            assert uploaded.json()["total_rows"] == 2
            assert uploaded.json()["review_required"] is True
            assert len(uploaded.json()["errors"]) == 1
            assert (tmp_path / uploaded.json()["stored_key"]).read_bytes() == csv_content

            image_catalogue = await client.post(
                "/api/catalogues",
                headers=headers,
                json={"supplier_id": supplier_id, "source_type": "manual"},
            )
            image_id = image_catalogue.json()["id"]
            image_upload = await client.post(
                f"/api/catalogues/{image_id}/upload",
                headers=headers,
                files={"file": ("design.jpg", b"image bytes", "image/jpeg")},
            )
            assert image_upload.status_code == 202
            assert image_upload.json()["items_created"] == 0
            assert image_upload.json()["review_required"] is True

            verified = await client.post(
                f"/api/catalogues/{catalogue_id}/verify",
                headers=headers,
                json={"verified": True},
            )
            assert verified.status_code == 200
            assert verified.json()["status"] == "active"
            assert verified.json()["last_verified_at"] is not None

        async with session_factory() as session:
            catalogue_uuid = UUID(catalogue_id)
            catalogue = await session.get(Catalogue, catalogue_uuid)
            item = await session.scalar(
                select(CatalogueItem).where(CatalogueItem.catalogue_id == catalogue_uuid)
            )
            image_record = await session.get(Catalogue, UUID(image_id))
            assert catalogue is not None
            assert catalogue.status.value == "active"
            assert item is not None
            assert item.fact_provenance["price"]["source"] == "supplier_catalogue"
            assert item.fact_provenance["price"]["source_id"] == catalogue_id
            assert item.last_verified_at is not None
            assert image_record is not None
            assert image_record.status.value == "needs_review"
            assert image_record.last_verified_at is None
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        await engine.dispose()