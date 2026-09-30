from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.extraction import RequirementExtractor
from app.agent.runner import ProcurementJobProcessor
from app.domain.models import Buyer, Conversation, Job, Message, ProcurementRequest
from app.domain.requirements import ProductCategory, Requirement
from app.domain.statuses import MessageDirection, MessageType, RequestStatus, SenderType
from app.infrastructure.db.base import Base
from app.infrastructure.db.jobs import enqueue_job
from app.workers.runner import DurableWorker


class FakeMediaLoader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def load_for_vision(
        self,
        media_id: str,
        existing_object_key: str | None = None,
    ) -> tuple[str, str]:
        self.calls.append((media_id, existing_object_key))
        return existing_object_key or "stored-image.jpg", "data:image/jpeg;base64,aW1hZ2U="


class CapturingExtractor(RequirementExtractor):
    def __init__(self) -> None:
        self.media_urls: list[str] = []

    async def extract(self, raw_message: str, media_urls: list[str]) -> Requirement:
        self.media_urls = media_urls
        return Requirement(
            category=ProductCategory.LADIES_SUIT,
            quantity=100,
            delivery_city="Jaipur",
        )


@pytest.mark.asyncio
async def test_request_worker_stores_and_passes_cloud_media_to_vision() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        buyer = Buyer(whatsapp_number="+919000000008", city="Jaipur", state="Rajasthan")
        session.add(buyer)
        await session.flush()
        conversation = Conversation(
            buyer_id=buyer.id,
            channel="whatsapp",
            external_conversation_id=buyer.whatsapp_number,
        )
        session.add(conversation)
        await session.flush()
        message = Message(
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND,
            sender_type=SenderType.BUYER,
            message_type=MessageType.IMAGE,
            text="Something like this",
            external_media_id="cloud-media-8",
            external_message_id="cloud-message-8",
        )
        session.add(message)
        await session.flush()
        request = ProcurementRequest(
            buyer_id=buyer.id,
            conversation_id=conversation.id,
            source_message_id=message.id,
            raw_message=message.text,
        )
        session.add(request)
        await session.flush()
        job, _ = await enqueue_job(
            session,
            job_type="process_request",
            payload={"request_id": str(request.id)},
            idempotency_key="process-media-request-8",
        )
        await session.commit()
        job_id = job.id
        request_id = request.id
        message_id = message.id

    checkpointer = InMemorySaver()
    @asynccontextmanager
    async def checkpointer_factory() -> AsyncIterator[InMemorySaver]:
        yield checkpointer

    media_loader = FakeMediaLoader()
    extractor = CapturingExtractor()
    processor = ProcurementJobProcessor(
        session_factory,
        checkpointer_factory=checkpointer_factory,
        extractor=extractor,
        media_loader=media_loader,
    )
    worker = DurableWorker(session_factory, processor)

    assert await worker.run_once()
    assert media_loader.calls == [("cloud-media-8", None)]
    assert extractor.media_urls == ["data:image/jpeg;base64,aW1hZ2U="]
    async with session_factory() as session:
        stored_message = await session.get(Message, message_id)
        stored_request = await session.get(ProcurementRequest, request_id)
        stored_job = await session.get(Job, job_id)
        assert stored_message is not None
        assert stored_message.external_media_id == "cloud-media-8"
        assert stored_message.media_object_key == "stored-image.jpg"
        assert stored_request is not None
        assert stored_request.status is RequestStatus.NEEDS_CLARIFICATION
        assert stored_job is not None
    await engine.dispose()