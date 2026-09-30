import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.models import (
    RFQ,
    Buyer,
    CatalogueItem,
    ProcurementRequest,
    Quote,
    Supplier,
    SupplierResponse,
)
from app.infrastructure.db.base import Base
from app.seed import seed_database


@pytest.mark.asyncio
async def test_seed_creates_fictional_demo_dataset_idempotently() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    first = await seed_database(session_factory)
    second = await seed_database(session_factory)

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Supplier)) == 20
        assert await session.scalar(select(func.count()).select_from(CatalogueItem)) == 100
        assert await session.scalar(select(func.count()).select_from(Buyer)) == 5
        assert await session.scalar(select(func.count()).select_from(ProcurementRequest)) == 1
        assert await session.scalar(select(func.count()).select_from(RFQ)) == 1
        assert await session.scalar(select(func.count()).select_from(SupplierResponse)) == 4
        assert await session.scalar(select(func.count()).select_from(Quote)) == 4

    assert first == {
        "suppliers": 20,
        "catalogue_items": 100,
        "buyers": 5,
        "demo_rfq_created": 1,
    }
    assert second["demo_rfq_created"] == 0
    await engine.dispose()