from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.infrastructure.db.base import Base
from app.infrastructure.db.session import get_session
from app.main import app
from app.settings import Settings, get_settings


@pytest.mark.asyncio
async def test_supplier_management_requires_auth_and_persists_records() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[object]:
        async with session_factory() as session:
            yield session

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(operator_api_token="test-token")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            payload = {
                "business_name": "Surat Suit House",
                "city": "Surat",
                "state": "Gujarat",
            }
            denied = await client.post("/api/suppliers", json=payload)
            assert denied.status_code == 401

            headers = {"X-Operator-Token": "test-token"}
            created = await client.post("/api/suppliers", json=payload, headers=headers)
            assert created.status_code == 201
            assert created.json()["business_name"] == "Surat Suit House"

            listed = await client.get("/api/suppliers", headers=headers)
            assert listed.status_code == 200
            assert [supplier["business_name"] for supplier in listed.json()] == [
                "Surat Suit House"
            ]

            supplier_id = created.json()["id"]
            fetched = await client.get(f"/api/suppliers/{supplier_id}", headers=headers)
            assert fetched.status_code == 200
            assert fetched.json()["city"] == "Surat"

            missing = await client.get(
                "/api/suppliers/00000000-0000-0000-0000-000000000001",
                headers=headers,
            )
            assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        await engine.dispose()