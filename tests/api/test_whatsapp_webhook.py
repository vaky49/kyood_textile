from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.models import (
    RFQ,
    Buyer,
    Conversation,
    Job,
    Message,
    Negotiation,
    ProcurementRequest,
    RFQSupplier,
    Supplier,
    SupplierResponse,
)
from app.domain.statuses import (
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
from app.infrastructure.db.session import get_session
from app.main import app
from app.settings import Settings, get_settings


@asynccontextmanager
async def webhook_test_client(
    settings: Settings,
) -> AsyncGenerator[tuple[AsyncClient, AnySessionFactory], None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[object]:
        async with session_factory() as session:
            yield session

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client, session_factory
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        await engine.dispose()


AnySessionFactory = async_sessionmaker


@pytest.mark.asyncio
async def test_duplicate_webhook_creates_a_single_request_and_job() -> None:
    async with webhook_test_client(Settings(app_env="development")) as (client, session_factory):
        payload = {
            "external_message_id": "wamid-1",
            "sender_phone": "+919876543210",
            "profile_name": "Buyer One",
            "text": "Need 100 cotton ladies suits",
            "message_type": "text",
        }

        first = await client.post("/webhooks/whatsapp", json=payload)
        duplicate = await client.post("/webhooks/whatsapp", json=payload)

        assert first.status_code == 202
        assert first.json()["duplicate"] is False
        assert first.json()["request_id"]
        assert duplicate.status_code == 202
        assert duplicate.json()["duplicate"] is True
        assert duplicate.json()["request_id"] is None

        async with session_factory() as session:
            message_count = await session.scalar(select(func.count()).select_from(Message))
            request_count = await session.scalar(select(func.count()).select_from(ProcurementRequest))
            job_count = await session.scalar(select(func.count()).select_from(Job))
            assert message_count == 1
            assert request_count == 1
            assert job_count == 1


@pytest.mark.asyncio
async def test_webhook_signature_and_get_challenge_are_verified() -> None:
    settings = Settings(
        app_env="production",
        whatsapp_app_secret="app-secret",
        whatsapp_verify_token="verify-token",
    )
    async with webhook_test_client(settings) as (client, _):
        payload = {
            "external_message_id": "wamid-secure",
            "sender_phone": "+919876543210",
            "text": "Need 100 cotton suits",
            "message_type": "text",
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        signature = "sha256=" + hmac.new(
            b"app-secret",
            body,
            hashlib.sha256,
        ).hexdigest()

        invalid = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=bad"},
        )
        accepted = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
        )
        challenge = await client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-token",
                "hub.challenge": "challenge-value",
            },
        )

        assert invalid.status_code == 401
        assert accepted.status_code == 202
        assert challenge.status_code == 200
        assert challenge.text == "challenge-value"


@pytest.mark.asyncio
async def test_supplier_reply_matches_open_rfq_and_is_idempotent() -> None:
    async with webhook_test_client(Settings(app_env="development")) as (client, session_factory):
        async with session_factory() as session:
            buyer = Buyer(whatsapp_number="+919000000001")
            supplier = Supplier(
                business_name="Surat Prints",
                whatsapp_number="+919000000002",
                city="Surat",
                state="Gujarat",
            )
            session.add_all([buyer, supplier])
            await session.flush()
            procurement_request = ProcurementRequest(buyer_id=buyer.id)
            session.add(procurement_request)
            await session.flush()
            rfq = RFQ(
                request_id=procurement_request.id,
                buyer_id=buyer.id,
                status=RFQStatus.WAITING_FOR_QUOTES,
                requirement_snapshot={"category": "ladies_suit"},
                idempotency_key="rfq:webhook-test",
                sent_at=datetime.now(UTC),
                deadline=datetime.now(UTC) + timedelta(hours=1),
            )
            session.add(rfq)
            await session.flush()
            rfq_supplier = RFQSupplier(
                rfq_id=rfq.id,
                supplier_id=supplier.id,
                status=RFQSupplierStatus.SENT,
                idempotency_key="rfq:webhook-test:supplier",
                sent_at=datetime.now(UTC),
            )
            session.add(rfq_supplier)
            await session.commit()
            expected_request_id = str(procurement_request.id)

        payload = {
            "external_message_id": "supplier-reply-1",
            "sender_phone": "+919000000002",
            "text": "₹560 per piece, 100 pcs minimum",
            "message_type": "text",
        }
        first = await client.post("/webhooks/whatsapp", json=payload)
        duplicate = await client.post("/webhooks/whatsapp", json=payload)

        assert first.status_code == 202
        assert first.json() == {
            "accepted": True,
            "duplicate": False,
            "request_id": expected_request_id,
        }
        assert duplicate.status_code == 202
        assert duplicate.json()["duplicate"] is True

        async with session_factory() as session:
            supplier_message = await session.scalar(
                select(Message).where(Message.external_message_id == "supplier-reply-1")
            )
            response = await session.scalar(select(SupplierResponse))
            jobs = list(await session.scalars(select(Job)))
            assert supplier_message is not None
            assert supplier_message.direction is MessageDirection.INBOUND
            assert supplier_message.sender_type is SenderType.SUPPLIER
            assert response is not None
            assert response.rfq_supplier_id == rfq_supplier.id
            assert response.status is SupplierResponseStatus.RECEIVED
            assert len(jobs) == 1
            assert jobs[0].job_type == "parse_supplier_response"


@pytest.mark.asyncio
async def test_unmatched_supplier_reply_is_saved_for_review_without_quote_job() -> None:
    async with webhook_test_client(Settings(app_env="development")) as (client, session_factory):
        async with session_factory() as session:
            supplier = Supplier(
                business_name="Ahmedabad Fabrics",
                whatsapp_number="+919000000003",
                city="Ahmedabad",
                state="Gujarat",
            )
            session.add(supplier)
            await session.commit()

        response = await client.post(
            "/webhooks/whatsapp",
            json={
                "external_message_id": "supplier-unmatched-1",
                "sender_phone": "+919000000003",
                "text": "495 + GST",
                "message_type": "text",
            },
        )

        assert response.status_code == 202
        async with session_factory() as session:
            supplier_response = await session.scalar(select(SupplierResponse))
            job_count = await session.scalar(select(func.count()).select_from(Job))
            assert supplier_response is not None
            assert supplier_response.rfq_supplier_id is None
            assert supplier_response.status is SupplierResponseStatus.NEEDS_REVIEW
            assert job_count == 0
@pytest.mark.asyncio
async def test_buyer_reply_is_attached_to_active_quote_request() -> None:
    async with webhook_test_client(Settings(app_env="development")) as (client, session_factory):
        async with session_factory() as session:
            buyer = Buyer(whatsapp_number="+919000000004")
            supplier = Supplier(business_name="Surat Selects", city="Surat", state="Gujarat")
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
                status=RequestStatus.AWAITING_BUYER_DECISION,
                comparison_payload={
                    "quote_count": 1,
                    "quotes": [{"selection_key": "A", "supplier_id": str(supplier.id)}],
                },
            )
            session.add(request)
            await session.commit()
            request_id = str(request.id)

        response = await client.post(
            "/webhooks/whatsapp",
            json={
                "external_message_id": "buyer-select-1",
                "sender_phone": "+919000000004",
                "text": "A",
                "message_type": "text",
            },
        )

        assert response.status_code == 202
        assert response.json()["request_id"] == request_id
        async with session_factory() as session:
            request_count = await session.scalar(select(func.count()).select_from(ProcurementRequest))
            jobs = list(await session.scalars(select(Job)))
            assert request_count == 1
            assert len(jobs) == 1
            assert jobs[0].job_type == "process_buyer_message"
            assert jobs[0].payload["request_id"] == request_id


@pytest.mark.asyncio
async def test_supplier_counteroffer_is_linked_to_pending_negotiation() -> None:
    async with webhook_test_client(Settings(app_env="development")) as (client, session_factory):
        async with session_factory() as session:
            buyer = Buyer(whatsapp_number="+919000000005")
            supplier = Supplier(
                business_name="Ahmedabad Weaves",
                whatsapp_number="+919000000006",
                city="Ahmedabad",
                state="Gujarat",
            )
            session.add_all([buyer, supplier])
            await session.flush()
            request = ProcurementRequest(buyer_id=buyer.id, status=RequestStatus.NEGOTIATING)
            session.add(request)
            await session.flush()
            rfq = RFQ(
                request_id=request.id,
                buyer_id=buyer.id,
                status=RFQStatus.WAITING_FOR_QUOTES,
                requirement_snapshot={"quantity": 100},
                idempotency_key="rfq:negotiation-webhook-test",
            )
            session.add(rfq)
            await session.flush()
            rfq_supplier = RFQSupplier(
                rfq_id=rfq.id,
                supplier_id=supplier.id,
                status=RFQSupplierStatus.RESPONDED,
                idempotency_key="rfq:negotiation-webhook-test:supplier",
            )
            session.add(rfq_supplier)
            await session.flush()
            buyer_conversation = Conversation(
                buyer_id=buyer.id,
                channel="whatsapp",
                external_conversation_id=buyer.whatsapp_number,
            )
            session.add(buyer_conversation)
            await session.flush()
            buyer_message = Message(
                conversation_id=buyer_conversation.id,
                direction=MessageDirection.INBOUND,
                sender_type=SenderType.BUYER,
                message_type=MessageType.TEXT,
                text="Negotiate A at INR 490",
                external_message_id="buyer-negotiate-webhook-test",
            )
            session.add(buyer_message)
            await session.flush()
            negotiation = Negotiation(
                request_id=request.id,
                rfq_supplier_id=rfq_supplier.id,
                buyer_message_id=buyer_message.id,
                target_price=490,
                target_currency="INR",
                status=NegotiationStatus.SENT,
                idempotency_key="negotiation:webhook-test",
            )
            session.add(negotiation)
            await session.commit()
            request_id = str(request.id)
            negotiation_id = negotiation.id
            rfq_supplier_id = rfq_supplier.id

        response = await client.post(
            "/webhooks/whatsapp",
            json={
                "external_message_id": "supplier-counteroffer-webhook-test",
                "sender_phone": "+919000000006",
                "text": "₹490 final",
                "message_type": "text",
            },
        )

        assert response.status_code == 202
        assert response.json()["request_id"] == request_id
        async with session_factory() as session:
            updated_negotiation = await session.get(Negotiation, negotiation_id)
            supplier_response = await session.scalar(select(SupplierResponse))
            assert updated_negotiation is not None
            assert updated_negotiation.response_message_id is not None
            assert supplier_response is not None
            assert supplier_response.rfq_supplier_id == rfq_supplier_id
            assert supplier_response.status is SupplierResponseStatus.RECEIVED


@pytest.mark.asyncio
async def test_meta_cloud_batch_ingests_text_and_image_messages() -> None:
    async with webhook_test_client(Settings(app_env="development")) as (client, session_factory):
        response = await client.post(
            "/webhooks/whatsapp",
            json={
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "contacts": [
                                        {
                                            "wa_id": "919000000007",
                                            "profile": {"name": "Cloud Buyer"},
                                        }
                                    ],
                                    "messages": [
                                        {
                                            "id": "cloud-text-1",
                                            "from": "919000000007",
                                            "type": "text",
                                            "text": {"body": "Need 100 cotton ladies suits"},
                                        },
                                        {
                                            "id": "cloud-image-1",
                                            "from": "919000000007",
                                            "type": "image",
                                            "image": {"id": "cloud-media-1", "caption": "Like this"},
                                        },
                                    ],
                                }
                            }
                        ]
                    }
                ],
            },
        )

        assert response.status_code == 202
        async with session_factory() as session:
            messages = list(await session.scalars(select(Message).order_by(Message.external_message_id)))
            requests = list(await session.scalars(select(ProcurementRequest)))
            jobs = list(await session.scalars(select(Job)))
            assert [message.external_message_id for message in messages] == [
                "cloud-image-1",
                "cloud-text-1",
            ]
            image_message = next(message for message in messages if message.external_message_id == "cloud-image-1")
            assert image_message.message_type is MessageType.IMAGE
            assert image_message.external_media_id == "cloud-media-1"
            assert image_message.text == "Like this"
            assert len(requests) == 2
            assert len(jobs) == 2
            assert all(job.job_type == "process_request" for job in jobs)