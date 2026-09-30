from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.models import (
    RFQ,
    Approval,
    Buyer,
    Conversation,
    Message,
    ProcurementRequest,
    Quote,
    RequirementVersion,
    RFQSupplier,
    Supplier,
    SupplierResponse,
)
from app.domain.statuses import (
    ApprovalType,
    MessageDirection,
    MessageType,
    RequestStatus,
    RFQStatus,
    RFQSupplierStatus,
    SenderType,
    SupplierResponseStatus,
)
from app.infrastructure.db.base import Base
from app.infrastructure.db.session import get_session
from app.main import app
from app.settings import Settings, get_settings


@pytest.mark.asyncio
async def test_request_and_rfq_views_include_comparison_and_source_message() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        buyer = Buyer(
            name="Retail Buyer",
            business_name="Jaipur Styles",
            whatsapp_number="+919000001111",
        )
        supplier = Supplier(
            business_name="Surat Cotton Works",
            city="Surat",
            state="Gujarat",
        )
        session.add_all([buyer, supplier])
        await session.flush()
        conversation = Conversation(
            buyer_id=buyer.id,
            channel="whatsapp",
            external_conversation_id=buyer.whatsapp_number,
        )
        session.add(conversation)
        await session.flush()
        request = ProcurementRequest(
            buyer_id=buyer.id,
            conversation_id=conversation.id,
            raw_message="Need 100 cotton suits around Rs 500-600",
            status=RequestStatus.AWAITING_BUYER_DECISION,
            comparison_payload={"quote_count": 1, "quotes": [{"supplier": supplier.business_name}]},
        )
        session.add(request)
        await session.flush()
        session.add(
            RequirementVersion(
                request_id=request.id,
                version=1,
                payload={"category": "ladies_suit", "quantity": 100},
                fact_provenance={"quantity": {"source": "buyer"}},
            )
        )
        buyer_message = Message(
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND,
            sender_type=SenderType.BUYER,
            message_type=MessageType.TEXT,
            text=request.raw_message,
            external_message_id="buyer-request-source",
        )
        session.add(buyer_message)
        rfq = RFQ(
            request_id=request.id,
            buyer_id=buyer.id,
            status=RFQStatus.QUOTES_READY,
            requirement_snapshot={"category": "ladies_suit", "quantity": 100},
            idempotency_key="rfq:request-view-test",
            sent_at=datetime.now(UTC),
            deadline=datetime.now(UTC) + timedelta(hours=4),
        )
        session.add(rfq)
        await session.flush()
        rfq_supplier = RFQSupplier(
            rfq_id=rfq.id,
            supplier_id=supplier.id,
            status=RFQSupplierStatus.RESPONDED,
            idempotency_key="rfq:request-view-test:supplier",
            sent_at=datetime.now(UTC),
            responded_at=datetime.now(UTC),
        )
        session.add(rfq_supplier)
        await session.flush()
        supplier_conversation = Conversation(
            supplier_id=supplier.id,
            channel="whatsapp_supplier",
            external_conversation_id="+919000002222",
        )
        session.add(supplier_conversation)
        await session.flush()
        source_message = Message(
            conversation_id=supplier_conversation.id,
            direction=MessageDirection.INBOUND,
            sender_type=SenderType.SUPPLIER,
            message_type=MessageType.TEXT,
            text="₹545 per piece, MOQ 50, plus GST",
            external_message_id="supplier-quote-source",
        )
        session.add(source_message)
        await session.flush()
        response = SupplierResponse(
            rfq_supplier_id=rfq_supplier.id,
            supplier_id=supplier.id,
            source_message_id=source_message.id,
            raw_text=source_message.text,
            parsed_payload={"price": "545", "currency": "INR"},
            status=SupplierResponseStatus.PARSED,
        )
        session.add(response)
        await session.flush()
        session.add(
            Quote(
                supplier_response_id=response.id,
                supplier_id=supplier.id,
                price=545,
                currency="INR",
                price_unit="piece",
                gst_included=False,
                moq=50,
                needs_human_review=False,
            )
        )
        session.add(
            Approval(
                request_id=request.id,
                approval_type=ApprovalType.SUPPLIER_SELECTION,
                thread_id=str(request.id),
                interrupt_key=f"{request.id}:select_suppliers",
                payload={"action": "select_suppliers"},
            )
        )
        await session.commit()
        request_id = str(request.id)
        rfq_id = str(rfq.id)

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
            denied = await client.get("/api/requests")
            assert denied.status_code == 401
            headers = {"X-Operator-Token": "test-token"}

            request_list = await client.get("/api/requests", headers=headers)
            request_detail = await client.get(f"/api/requests/{request_id}", headers=headers)
            rfq_list = await client.get("/api/rfqs", headers=headers)
            rfq_detail = await client.get(f"/api/rfqs/{rfq_id}", headers=headers)

        assert request_list.status_code == 200
        assert request_list.json()[0]["category"] == "ladies_suit"
        assert request_list.json()[0]["quantity"] == 100
        assert request_detail.status_code == 200
        assert request_detail.json()["comparison_payload"]["quote_count"] == 1
        assert request_detail.json()["requirement_versions"][0]["fact_provenance"]["quantity"][
            "source"
        ] == "buyer"
        assert request_detail.json()["messages"][0]["text"] == "Need 100 cotton suits around Rs 500-600"
        assert request_detail.json()["approvals"][0]["approval_type"] == "supplier_selection"
        assert rfq_list.status_code == 200
        assert rfq_list.json()[0]["supplier_count"] == 1
        assert rfq_detail.status_code == 200
        assert rfq_detail.json()["quotes"][0]["source_message_text"] == "₹545 per piece, MOQ 50, plus GST"
        assert rfq_detail.json()["quotes"][0]["gst_included"] is False
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        await engine.dispose()