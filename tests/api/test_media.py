from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.infrastructure.db.base import Base
from app.infrastructure.db.session import get_session
from app.infrastructure.storage.local import LocalMediaStorage
from app.main import app
from app.settings import Settings, get_settings


@pytest.mark.asyncio
async def test_media_download_requires_operator_auth_and_hides_storage_path(tmp_path) -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[object]:
        async with session_factory() as session:
            yield session

    storage = LocalMediaStorage(tmp_path / "media")
    object_key = await storage.save("source.jpg", "image/jpeg", b"private-image")
    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(
        operator_api_token="operator-token",
        media_storage_dir=str(tmp_path / "media"),
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            denied = await client.get(f"/api/media/{object_key}")
            fetched = await client.get(
                f"/api/media/{object_key}",
                headers={"X-Operator-Token": "operator-token"},
            )
            traversal = await client.get(
                "/api/media/%2E%2E%2Fsecret.txt",
                headers={"X-Operator-Token": "operator-token"},
            )

        assert denied.status_code == 401
        assert fetched.status_code == 200
        assert fetched.content == b"private-image"
        assert fetched.headers["content-type"] == "image/jpeg"
        assert fetched.headers["x-content-type-options"] == "nosniff"
        assert traversal.status_code == 404
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        await engine.dispose()