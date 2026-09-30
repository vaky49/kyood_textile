from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.runner import ProcurementJobProcessor
from app.domain.models import (
    RFQ,
    Approval,
    Buyer,
    Catalogue,
    CatalogueItem,
    Conversation,
    Message,
    Negotiation,
    ProcurementRequest,
    Quote,
    RFQSupplier,
    Supplier,
    SupplierResponse,
)
from app.domain.statuses import (
    ApprovalStatus,
    ApprovalType,
    CatalogueSourceType,
    CatalogueStatus,
    MessageDirection,
    MessageType,
    NegotiationStatus,
    RequestStatus,
    RFQStatus,
    RFQSupplierStatus,
    SenderType,
    SupplierResponseStatus,
)
from app.infrastructure.db.base import Base
from app.infrastructure.db.jobs import enqueue_job
from app.workers.runner import DurableWorker


@pytest.mark.asyncio
async def test_worker_runs_request_through_approved_rfq_and_persists_results() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        buyer = Buyer(
            whatsapp_number="+919876543210",
            city="Jaipur",
            state="Rajasthan",
        )
        supplier = Supplier(
            business_name="Ahmedabad Cotton House",
            city="Ahmedabad",
            state="Gujarat",
            is_verified=True,
        )
        session.add_all([buyer, supplier])
        await session.flush()
        catalogue = Catalogue(
            supplier_id=supplier.id,
            source_type=CatalogueSourceType.MANUAL,
            status=CatalogueStatus.ACTIVE,
            last_verified_at=datetime.now(UTC) - timedelta(days=2),
        )
        session.add(catalogue)
        await session.flush()
        session.add(
            CatalogueItem(
                catalogue_id=catalogue.id,
                supplier_id=supplier.id,
                title="Printed cotton ladies suit",
                category="ladies_suit",
                fabric="cotton",
                price=550,
                currency="INR",
                moq=50,
                last_verified_at=catalogue.last_verified_at,
            )
        )
        procurement_request = ProcurementRequest(
            buyer_id=buyer.id,
            raw_message="Need 100 cotton ladies suits around \u20b9500-600, delivery to Jaipur",
        )
        session.add(procurement_request)
        await session.commit()
        request_id = str(procurement_request.id)
        supplier_id = str(supplier.id)

    checkpointer = InMemorySaver()

    @asynccontextmanager
    async def checkpointer_factory() -> AsyncIterator[InMemorySaver]:
        yield checkpointer

    processor = ProcurementJobProcessor(
        session_factory,
        checkpointer_factory=checkpointer_factory,
    )
    worker = DurableWorker(session_factory, processor)
    async with session_factory() as session:
        await enqueue_job(
            session,
            job_type="process_request",
            payload={"request_id": request_id},
            idempotency_key=f"process-request:{request_id}",
        )
        await session.commit()
    assert await worker.run_once()

    async with session_factory() as session:
        selection = await session.scalar(
            select(Approval).where(Approval.approval_type == ApprovalType.SUPPLIER_SELECTION)
        )
        assert selection is not None
        assert selection.status is ApprovalStatus.PENDING
        assert selection.thread_id
        assert selection.payload["candidates"][0]["supplier_id"] == supplier_id
        assert (await session.get(ProcurementRequest, procurement_request.id)).status is (
            RequestStatus.AWAITING_SUPPLIER_SELECTION
        )
        selection.status = ApprovalStatus.APPROVED
        selection.decision_payload = {"selected_supplier_ids": [supplier_id]}
        selection_id = str(selection.id)
        await enqueue_job(
            session,
            job_type="resume_graph",
            payload={"request_id": request_id, "approval_id": selection_id},
            idempotency_key=f"resume-approval:{selection_id}",
        )
        await session.commit()

    assert await worker.run_once()

    async with session_factory() as session:
        rfq_approval = await session.scalar(
            select(Approval).where(Approval.approval_type == ApprovalType.RFQ)
        )
        rfq = await session.scalar(select(RFQ))
        rfq_supplier = await session.scalar(select(RFQSupplier))
        assert rfq_approval is not None
        assert rfq_approval.status is ApprovalStatus.PENDING
        assert rfq is not None
        assert rfq.status is RFQStatus.AWAITING_APPROVAL
        assert rfq_supplier is not None
        assert rfq_supplier.supplier_id == supplier.id
        rfq_approval.status = ApprovalStatus.APPROVED
        rfq_approval.decision_payload = {"approved": True}
        rfq_approval_id = str(rfq_approval.id)
        await enqueue_job(
            session,
            job_type="resume_graph",
            payload={"request_id": request_id, "approval_id": rfq_approval_id},
            idempotency_key=f"resume-approval:{rfq_approval_id}",
        )
        await session.commit()

    assert await worker.run_once()

    async with session_factory() as session:
        procurement_request = await session.get(ProcurementRequest, procurement_request.id)
        rfq = await session.scalar(select(RFQ))
        rfq_supplier = await session.scalar(select(RFQSupplier))
        assert procurement_request is not None
        assert procurement_request.status is RequestStatus.WAITING_FOR_QUOTES
        assert rfq is not None
        assert rfq.status.value == "waiting_for_quotes"
        assert rfq_supplier is not None
        assert rfq_supplier.status is RFQSupplierStatus.SENT
        supplier_conversation = Conversation(
            supplier_id=supplier.id,
            channel="whatsapp_supplier",
            external_conversation_id="+919876543212",
        )
        session.add(supplier_conversation)
        await session.flush()
        source_message = Message(
            conversation_id=supplier_conversation.id,
            direction=MessageDirection.INBOUND,
            sender_type=SenderType.SUPPLIER,
            message_type=MessageType.TEXT,
            text="₹560 per piece, MOQ: 100 pcs, dispatch tomorrow",
            external_message_id="supplier-quote-1",
        )
        session.add(source_message)
        await session.flush()
        supplier_response = SupplierResponse(
            rfq_supplier_id=rfq_supplier.id,
            supplier_id=supplier.id,
            source_message_id=source_message.id,
            raw_text=source_message.text,
            status=SupplierResponseStatus.RECEIVED,
        )
        session.add(supplier_response)
        await session.flush()
        await enqueue_job(
            session,
            job_type="parse_supplier_response",
            payload={"supplier_response_id": str(supplier_response.id)},
            idempotency_key="parse-supplier-response:supplier-quote-1",
        )
        await session.commit()

    assert await worker.run_once()
    assert await worker.run_once()

    async with session_factory() as session:
        updated_request = await session.get(ProcurementRequest, procurement_request.id)
        rfq = await session.scalar(select(RFQ))
        rfq_supplier = await session.scalar(select(RFQSupplier))
        quote = await session.scalar(select(Quote))
        buyer_message = await session.scalar(
            select(Message).where(Message.external_message_id == f"comparison:{rfq.id}")
        )
        assert updated_request is not None
        assert updated_request.status is RequestStatus.AWAITING_BUYER_DECISION
        assert updated_request.comparison_payload is not None
        assert updated_request.comparison_payload["quote_count"] == 1
        assert updated_request.comparison_payload["quotes"][0]["source_message_id"] == str(
            source_message.id
        )
        assert rfq is not None
        assert rfq.status is RFQStatus.QUOTES_READY
        assert rfq_supplier is not None
        assert rfq_supplier.status is RFQSupplierStatus.RESPONDED
        assert quote is not None
        assert str(quote.price) == "560.00"
        assert quote.currency == "INR"
        assert quote.moq == 100
        assert quote.available_quantity is None
        assert buyer_message is not None
        assert "GST status not specified" in buyer_message.text
        assert "Stock reported: not specified" in buyer_message.text

        buyer_conversation = await session.scalar(
            select(Conversation).where(
                Conversation.buyer_id == buyer.id,
                Conversation.channel == "whatsapp",
            )
        )
        assert buyer_conversation is not None
        negotiate_message = Message(
            conversation_id=buyer_conversation.id,
            direction=MessageDirection.INBOUND,
            sender_type=SenderType.BUYER,
            message_type=MessageType.TEXT,
            text="Negotiate A at INR 490",
            external_message_id="buyer-negotiate-1",
        )
        session.add(negotiate_message)
        await session.flush()
        await enqueue_job(
            session,
            job_type="process_buyer_message",
            payload={"request_id": request_id, "message_id": str(negotiate_message.id)},
            idempotency_key="process-buyer-message:buyer-negotiate-1",
        )
        await session.commit()

    assert await worker.run_once()
    assert await worker.run_once()

    async with session_factory() as session:
        negotiation_approval = await session.scalar(
            select(Approval).where(Approval.approval_type == ApprovalType.NEGOTIATION)
        )
        negotiation = await session.scalar(select(Negotiation))
        assert negotiation_approval is not None
        assert negotiation_approval.status is ApprovalStatus.PENDING
        assert negotiation is not None
        assert negotiation.status is NegotiationStatus.AWAITING_APPROVAL
        assert str(negotiation.target_price) == "490.00"
        assert negotiation.target_currency == "INR"
        negotiation_approval.status = ApprovalStatus.APPROVED
        negotiation_approval.decision_payload = {"approved": True}
        negotiation_approval_id = str(negotiation_approval.id)
        await enqueue_job(
            session,
            job_type="resume_graph",
            payload={"request_id": request_id, "approval_id": negotiation_approval_id},
            idempotency_key=f"resume-approval:{negotiation_approval_id}",
        )
        await session.commit()

    assert await worker.run_once()

    async with session_factory() as session:
        negotiation = await session.scalar(select(Negotiation))
        supplier_conversation = await session.scalar(
            select(Conversation).where(
                Conversation.supplier_id == supplier.id,
                Conversation.channel == "whatsapp_supplier",
            )
        )
        assert negotiation is not None
        assert negotiation.status is NegotiationStatus.SENT
        assert supplier_conversation is not None
        negotiation_message = await session.scalar(
            select(Message).where(Message.external_message_id == f"negotiation:{negotiation.id}")
        )
        assert negotiation_message is not None
        assert "INR 490.00" in negotiation_message.text
        counteroffer = Message(
            conversation_id=supplier_conversation.id,
            direction=MessageDirection.INBOUND,
            sender_type=SenderType.SUPPLIER,
            message_type=MessageType.TEXT,
            text="₹490 final",
            external_message_id="supplier-counteroffer-1",
        )
        session.add(counteroffer)
        await session.flush()
        counteroffer_response = SupplierResponse(
            rfq_supplier_id=rfq_supplier.id,
            supplier_id=supplier.id,
            source_message_id=counteroffer.id,
            raw_text=counteroffer.text,
            status=SupplierResponseStatus.RECEIVED,
        )
        session.add(counteroffer_response)
        await session.flush()
        negotiation.response_message_id = counteroffer.id
        await enqueue_job(
            session,
            job_type="parse_supplier_response",
            payload={"supplier_response_id": str(counteroffer_response.id)},
            idempotency_key="parse-supplier-response:supplier-counteroffer-1",
        )
        await session.commit()

    assert await worker.run_once()

    async with session_factory() as session:
        negotiation = await session.scalar(select(Negotiation))
        request_after_counteroffer = await session.get(ProcurementRequest, procurement_request.id)
        assert negotiation is not None
        assert negotiation.status is NegotiationStatus.RESPONDED
        assert str(negotiation.supplier_price) == "490.00"
        assert negotiation.supplier_currency == "INR"
        assert request_after_counteroffer is not None
        assert request_after_counteroffer.status is RequestStatus.AWAITING_BUYER_DECISION
        buyer_conversation = await session.scalar(
            select(Conversation).where(
                Conversation.buyer_id == buyer.id,
                Conversation.channel == "whatsapp",
            )
        )
        assert buyer_conversation is not None
        confirm_message = Message(
            conversation_id=buyer_conversation.id,
            direction=MessageDirection.INBOUND,
            sender_type=SenderType.BUYER,
            message_type=MessageType.TEXT,
            text="Confirm",
            external_message_id="buyer-confirm-1",
        )
        session.add(confirm_message)
        await session.flush()
        await enqueue_job(
            session,
            job_type="process_buyer_message",
            payload={"request_id": request_id, "message_id": str(confirm_message.id)},
            idempotency_key="process-buyer-message:buyer-confirm-1",
        )
        await session.commit()

    assert await worker.run_once()
    assert await worker.run_once()

    async with session_factory() as session:
        order_approval = await session.scalar(
            select(Approval).where(Approval.approval_type == ApprovalType.ORDER_CONFIRMATION)
        )
        assert order_approval is not None
        assert order_approval.status is ApprovalStatus.PENDING
        order_approval.status = ApprovalStatus.APPROVED
        order_approval.decision_payload = {"approved": True}
        order_approval_id = str(order_approval.id)
        await enqueue_job(
            session,
            job_type="resume_graph",
            payload={"request_id": request_id, "approval_id": order_approval_id},
            idempotency_key=f"resume-approval:{order_approval_id}",
        )
        await session.commit()

    assert await worker.run_once()
    async with session_factory() as session:
        completed_request = await session.get(ProcurementRequest, procurement_request.id)
        assert completed_request is not None
        assert completed_request.status is RequestStatus.ORDER_CONFIRMED

    await engine.dispose()