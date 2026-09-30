from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.graph import SupplierMessagingError
from app.domain.matching import ProductCategory
from app.domain.models import OutboundDelivery, Supplier
from app.domain.requirements import Requirement
from app.domain.statuses import DeliveryStatus
from app.infrastructure.db.base import Base
from app.infrastructure.messaging.mock import MockMessagingProvider
from app.infrastructure.messaging.workflow import WorkflowMessagingProvider, _format_rfq


@pytest.mark.asyncio
async def test_workflow_provider_resolves_supplier_and_formats_rfq() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    supplier_id = uuid4()
    async with session_factory() as session:
        session.add(
            Supplier(
                id=supplier_id,
                business_name="Surat House",
                whatsapp_number="+919876543210",
                city="Surat",
                state="Gujarat",
            )
        )
        await session.commit()

    transport = MockMessagingProvider()
    provider = WorkflowMessagingProvider(session_factory, transport)
    requirement = Requirement(
        category=ProductCategory.LADIES_SUIT,
        fabric="cotton",
        quantity=100,
        delivery_city="Jaipur",
    )
    await provider.send_rfq(str(supplier_id), requirement, "rfq:1:supplier:1")
    await provider.send_rfq(str(supplier_id), requirement, "rfq:1:supplier:1")

    assert len(transport.sent_messages) == 1
    assert transport.sent_messages[0].recipient == "+919876543210"
    assert "Quantity: 100 pieces" in transport.sent_messages[0].text
    assert "Delivery destination: Jaipur" in transport.sent_messages[0].text
    assert "Target:" not in transport.sent_messages[0].text
    async with session_factory() as session:
        delivery = await session.scalar(select(OutboundDelivery))
        assert delivery is not None
        assert delivery.status is DeliveryStatus.SENT
        assert delivery.attempt_count == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_ambiguous_in_flight_delivery_is_not_resent() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            OutboundDelivery(
                idempotency_key="rfq:ambiguous",
                recipient="+919876543210",
                channel="whatsapp",
                body_text="RFQ",
                status=DeliveryStatus.SENDING,
                attempt_count=1,
            )
        )
        await session.commit()
    transport = MockMessagingProvider()
    provider = WorkflowMessagingProvider(session_factory, transport)

    with pytest.raises(SupplierMessagingError, match="operator review"):
        await provider.send_message("+919876543210", "RFQ", "rfq:ambiguous")

    assert transport.sent_messages == []
    async with session_factory() as session:
        delivery = await session.scalar(select(OutboundDelivery))
        assert delivery is not None
        assert delivery.status is DeliveryStatus.NEEDS_REVIEW
    await engine.dispose()


def test_rfq_formatter_never_invents_optional_commercial_terms() -> None:
    text = _format_rfq(Requirement(category=ProductCategory.LADIES_SUIT, quantity=100))

    assert "Category: ladies_suit" in text
    assert "Quantity: 100 pieces" in text
    assert "Fabric:" not in text
    assert "Target:" not in text