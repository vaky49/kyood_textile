import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    RFQ,
    Buyer,
    Conversation,
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
from app.infrastructure.db.jobs import enqueue_job
from app.infrastructure.db.session import get_session
from app.settings import Settings, get_settings

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])
DatabaseSession = Annotated[AsyncSession, Depends(get_session)]
ApplicationSettings = Annotated[Settings, Depends(get_settings)]


class IncomingWhatsAppMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_message_id: str = Field(min_length=1, max_length=200)
    sender_phone: str = Field(min_length=5, max_length=32)
    profile_name: str | None = Field(default=None, max_length=200)
    text: str | None = None
    message_type: MessageType = MessageType.TEXT
    media_url: str | None = None
    media_id: str | None = None


class WebhookAccepted(BaseModel):
    accepted: bool = True
    duplicate: bool
    request_id: str | None = None


@router.get("", response_class=PlainTextResponse)
async def verify_webhook(
    mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
    settings: ApplicationSettings = None,
) -> str:
    if (
        mode != "subscribe"
        or settings.whatsapp_verify_token is None
        or verify_token is None
        or challenge is None
        or not hmac.compare_digest(verify_token, settings.whatsapp_verify_token)
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Webhook verification failed")
    return challenge


@router.post("", response_model=WebhookAccepted, status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(
    request: Request,
    session: DatabaseSession,
    settings: ApplicationSettings,
    signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> WebhookAccepted:
    body = await request.body()
    _verify_signature(body, signature, settings)
    try:
        payload = json.loads(body)
        messages = _decode_webhook_payload(payload)
    except (json.JSONDecodeError, ValidationError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unsupported or invalid WhatsApp webhook payload",
        ) from error
    if not messages:
        return WebhookAccepted(duplicate=False)

    results: list[WebhookAccepted] = []
    for message in messages:
        if not (message.text and message.text.strip()) and message.media_url is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="A text or media payload is required",
            )
        results.append(await _ingest_message(session, message))
    request_ids = [result.request_id for result in results if result.request_id is not None]
    return WebhookAccepted(
        duplicate=all(result.duplicate for result in results),
        request_id=request_ids[0] if request_ids else None,
    )


def _decode_webhook_payload(payload: Any) -> list[IncomingWhatsAppMessage]:
    if isinstance(payload, dict) and "external_message_id" in payload:
        return [IncomingWhatsAppMessage.model_validate(payload)]
    if not isinstance(payload, dict):
        raise TypeError("Webhook body must be a JSON object")

    normalized: list[IncomingWhatsAppMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            contacts = {
                contact.get("wa_id"): contact
                for contact in value.get("contacts", [])
                if contact.get("wa_id")
            }
            for message in value.get("messages", []):
                message_type = message.get("type", "text")
                message_text: str | None = None
                media_url: str | None = None
                media_id: str | None = None
                if message_type == "text":
                    message_text = message.get("text", {}).get("body")
                elif message_type in {"image", "document", "audio", "video"}:
                    media = message.get(message_type, {})
                    media_id = media.get("id")
                    message_text = media.get("caption")
                elif message_type == "interactive":
                    interactive = message.get("interactive", {})
                    message_text = (
                        interactive.get("button_reply", {}).get("title")
                        or interactive.get("list_reply", {}).get("title")
                    )
                    message_type = "text"
                elif message_type == "button":
                    message_text = message.get("button", {}).get("text")
                    message_type = "text"
                elif message_type == "location":
                    location = message.get("location", {})
                    latitude = location.get("latitude")
                    longitude = location.get("longitude")
                    if latitude is not None and longitude is not None:
                        message_text = f"Location coordinates: {latitude}, {longitude}"
                else:
                    continue

                sender = message.get("from")
                if not sender or not message.get("id"):
                    continue
                contact = contacts.get(sender, {})
                profile_name = contact.get("profile", {}).get("name")
                normalized.append(
                    IncomingWhatsAppMessage(
                        external_message_id=message["id"],
                        sender_phone=f"+{sender}" if not sender.startswith("+") else sender,
                        profile_name=profile_name,
                        text=message_text,
                        message_type=MessageType(message_type),
                        media_url=media_url,
                        media_id=media_id,
                    )
                )
    return normalized


async def _ingest_message(
    session: AsyncSession,
    message: IncomingWhatsAppMessage,
) -> WebhookAccepted:
    dialect = session.get_bind().dialect.name
    insert = postgres_insert if dialect == "postgresql" else sqlite_insert
    supplier = await session.scalar(
        select(Supplier).where(Supplier.whatsapp_number == message.sender_phone)
    )
    if supplier is not None:
        return await _receive_supplier_message(session, message, supplier, insert)

    buyer_id = uuid4()
    buyer_insert = (
        insert(Buyer)
        .values(
            id=buyer_id,
            whatsapp_number=message.sender_phone,
            name=message.profile_name,
        )
        .on_conflict_do_nothing(index_elements=[Buyer.whatsapp_number])
        .returning(Buyer.id)
    )
    inserted_buyer_id = (await session.execute(buyer_insert)).scalar_one_or_none()
    if inserted_buyer_id is None:
        buyer_id = await session.scalar(
            select(Buyer.id).where(Buyer.whatsapp_number == message.sender_phone)
        )
        if buyer_id is None:
            raise RuntimeError("Buyer upsert conflicted but the existing buyer was not found")
    else:
        buyer_id = inserted_buyer_id

    conversation_id = uuid4()
    conversation_insert = (
        insert(Conversation)
        .values(
            id=conversation_id,
            buyer_id=buyer_id,
            channel="whatsapp",
            external_conversation_id=message.sender_phone,
        )
        .on_conflict_do_nothing(
            index_elements=[Conversation.channel, Conversation.external_conversation_id]
        )
        .returning(Conversation.id)
    )
    inserted_conversation_id = (await session.execute(conversation_insert)).scalar_one_or_none()
    if inserted_conversation_id is None:
        conversation_id = await session.scalar(
            select(Conversation.id).where(
                Conversation.channel == "whatsapp",
                Conversation.external_conversation_id == message.sender_phone,
            )
        )
        if conversation_id is None:
            raise RuntimeError("Conversation upsert conflicted but the existing record was not found")
    else:
        conversation_id = inserted_conversation_id

    message_id = uuid4()
    message_insert = (
        insert(Message)
        .values(
            id=message_id,
            conversation_id=conversation_id,
            direction=MessageDirection.INBOUND,
            sender_type=SenderType.BUYER,
            message_type=message.message_type,
            text=message.text,
            media_url=message.media_url,
            external_media_id=message.media_id,
            external_message_id=message.external_message_id,
        )
        .on_conflict_do_nothing(index_elements=[Message.external_message_id])
        .returning(Message.id)
    )
    inserted_message_id = (await session.execute(message_insert)).scalar_one_or_none()
    if inserted_message_id is None:
        await session.rollback()
        return WebhookAccepted(duplicate=True)

    active_request = await session.scalar(
        select(ProcurementRequest)
        .where(
            ProcurementRequest.buyer_id == buyer_id,
            ProcurementRequest.status == RequestStatus.AWAITING_BUYER_DECISION,
            ProcurementRequest.comparison_payload.is_not(None),
        )
        .order_by(ProcurementRequest.updated_at.desc(), ProcurementRequest.id)
        .limit(1)
        .with_for_update()
    )
    if active_request is not None:
        await enqueue_job(
            session,
            job_type="process_buyer_message",
            payload={
                "request_id": str(active_request.id),
                "message_id": str(inserted_message_id),
            },
            idempotency_key=f"process-buyer-message:{message.external_message_id}",
        )
        await session.commit()
        return WebhookAccepted(duplicate=False, request_id=str(active_request.id))

    procurement_request = ProcurementRequest(
        buyer_id=buyer_id,
        conversation_id=conversation_id,
        source_message_id=inserted_message_id,
        raw_message=message.text,
        status=RequestStatus.NEW,
    )
    session.add(procurement_request)
    await session.flush()
    await enqueue_job(
        session,
        job_type="process_request",
        payload={"request_id": str(procurement_request.id)},
        idempotency_key=f"process-message:{message.external_message_id}",
    )
    await session.commit()
    return WebhookAccepted(duplicate=False, request_id=str(procurement_request.id))


async def _receive_supplier_message(
    session: AsyncSession,
    message: IncomingWhatsAppMessage,
    supplier: Supplier,
    insert: Any,
) -> WebhookAccepted:
    negotiation = await session.scalar(
        select(Negotiation)
        .join(RFQSupplier, RFQSupplier.id == Negotiation.rfq_supplier_id)
        .where(
            RFQSupplier.supplier_id == supplier.id,
            Negotiation.status == NegotiationStatus.SENT,
        )
        .order_by(Negotiation.created_at.desc(), Negotiation.id)
        .limit(1)
    )
    if negotiation is not None:
        rfq_supplier = await session.get(RFQSupplier, negotiation.rfq_supplier_id)
        rfq = await session.get(RFQ, rfq_supplier.rfq_id) if rfq_supplier is not None else None
    else:
        rfq_result = await session.execute(
            select(RFQSupplier, RFQ)
            .join(RFQ, RFQ.id == RFQSupplier.rfq_id)
            .where(
                RFQSupplier.supplier_id == supplier.id,
                RFQSupplier.status == RFQSupplierStatus.SENT,
                RFQ.status.in_([RFQStatus.SENT, RFQStatus.WAITING_FOR_QUOTES]),
                or_(RFQ.deadline.is_(None), RFQ.deadline >= datetime.now(UTC)),
            )
            .order_by(RFQ.sent_at.desc(), RFQ.created_at.desc())
            .limit(1)
        )
        linked_rfq = rfq_result.first()
        rfq_supplier, rfq = linked_rfq if linked_rfq is not None else (None, None)

    conversation_id = uuid4()
    conversation_insert = (
        insert(Conversation)
        .values(
            id=conversation_id,
            supplier_id=supplier.id,
            channel="whatsapp_supplier",
            external_conversation_id=message.sender_phone,
        )
        .on_conflict_do_nothing(
            index_elements=[Conversation.channel, Conversation.external_conversation_id]
        )
        .returning(Conversation.id)
    )
    inserted_conversation_id = (await session.execute(conversation_insert)).scalar_one_or_none()
    if inserted_conversation_id is None:
        conversation_id = await session.scalar(
            select(Conversation.id).where(
                Conversation.channel == "whatsapp_supplier",
                Conversation.external_conversation_id == message.sender_phone,
            )
        )
        if conversation_id is None:
            raise RuntimeError("Supplier conversation upsert conflicted but record was not found")
    else:
        conversation_id = inserted_conversation_id

    message_insert = (
        insert(Message)
        .values(
            id=uuid4(),
            conversation_id=conversation_id,
            direction=MessageDirection.INBOUND,
            sender_type=SenderType.SUPPLIER,
            message_type=message.message_type,
            text=message.text,
            media_url=message.media_url,
            external_media_id=message.media_id,
            external_message_id=message.external_message_id,
        )
        .on_conflict_do_nothing(index_elements=[Message.external_message_id])
        .returning(Message.id)
    )
    source_message_id = (await session.execute(message_insert)).scalar_one_or_none()
    if source_message_id is None:
        await session.rollback()
        return WebhookAccepted(duplicate=True)

    supplier_response = SupplierResponse(
        rfq_supplier_id=rfq_supplier.id if rfq_supplier is not None else None,
        supplier_id=supplier.id,
        source_message_id=source_message_id,
        raw_text=message.text,
        status=(
            SupplierResponseStatus.RECEIVED
            if rfq_supplier is not None
            else SupplierResponseStatus.NEEDS_REVIEW
        ),
    )
    session.add(supplier_response)
    await session.flush()
    if rfq_supplier is not None:
        if negotiation is not None:
            negotiation.response_message_id = source_message_id
        await enqueue_job(
            session,
            job_type="parse_supplier_response",
            payload={"supplier_response_id": str(supplier_response.id)},
            idempotency_key=f"parse-supplier-response:{message.external_message_id}",
        )
    await session.commit()
    request_id = str(rfq.request_id) if rfq is not None else None
    return WebhookAccepted(duplicate=False, request_id=request_id)


def _verify_signature(body: bytes, signature: str | None, settings: Settings) -> None:
    if settings.whatsapp_app_secret is None:
        if settings.app_env == "production":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="WhatsApp signature verification is not configured",
            )
        return

    if signature is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing webhook signature")
    expected = "sha256=" + hmac.new(
        settings.whatsapp_app_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")