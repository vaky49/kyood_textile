from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.graph import SupplierMessagingError
from app.domain.models import OutboundDelivery, Supplier
from app.domain.requirements import Requirement
from app.domain.statuses import DeliveryStatus
from app.infrastructure.messaging.mock import MockMessagingProvider
from app.infrastructure.messaging.protocols import MessagingProvider
from app.infrastructure.messaging.whatsapp_cloud import (
    MessagingSendError,
    WhatsAppCloudApiClient,
    WhatsAppMessagingProvider,
)
from app.settings import Settings


class WorkflowMessagingProvider:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        transport: MessagingProvider,
    ) -> None:
        self._session_factory = session_factory
        self._transport = transport

    async def send_message(self, phone_number: str, text: str, idempotency_key: str) -> None:
        await self._deliver(phone_number, text, idempotency_key)

    async def send_rfq(
        self,
        supplier_id: str,
        requirement: Requirement,
        idempotency_key: str,
    ) -> None:
        supplier = await self._supplier(supplier_id)
        text = _format_rfq(requirement)
        await self._deliver(supplier.whatsapp_number or "", text, idempotency_key)

    async def send_negotiation(
        self,
        supplier_id: str,
        target_price: str,
        currency: str,
        idempotency_key: str,
    ) -> None:
        supplier = await self._supplier(supplier_id)
        text = f"Can you offer {currency} {target_price} for the requested quantity? Please confirm your final price and GST terms."
        await self._deliver(supplier.whatsapp_number or "", text, idempotency_key)

    async def _deliver(self, recipient: str, text: str, idempotency_key: str) -> None:
        async with self._session_factory() as session:
            delivery = await session.scalar(
                select(OutboundDelivery)
                .where(OutboundDelivery.idempotency_key == idempotency_key)
                .with_for_update()
            )
            if delivery is None:
                delivery = OutboundDelivery(
                    id=uuid4(),
                    idempotency_key=idempotency_key,
                    recipient=recipient,
                    channel="whatsapp",
                    body_text=text,
                    status=DeliveryStatus.SENDING,
                    attempt_count=1,
                )
                session.add(delivery)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    raise SupplierMessagingError("Outbound delivery is already being processed")
            elif delivery.status is DeliveryStatus.SENT:
                return
            else:
                delivery.status = DeliveryStatus.NEEDS_REVIEW
                delivery.error_summary = "A prior send attempt has no confirmed provider receipt"
                await session.commit()
                raise SupplierMessagingError("Prior outbound attempt requires operator review")

        try:
            await self._transport.send_text(recipient, text, idempotency_key)
        except MessagingSendError as error:
            async with self._session_factory() as session:
                delivery = await session.scalar(
                    select(OutboundDelivery).where(
                        OutboundDelivery.idempotency_key == idempotency_key
                    )
                )
                if delivery is not None:
                    delivery.status = DeliveryStatus.NEEDS_REVIEW
                    delivery.error_summary = str(error)[:2000]
                    await session.commit()
            raise SupplierMessagingError(str(error)) from error

        async with self._session_factory() as session:
            delivery = await session.scalar(
                select(OutboundDelivery).where(OutboundDelivery.idempotency_key == idempotency_key)
            )
            if delivery is None:
                raise SupplierMessagingError("Outbound delivery record disappeared after send")
            delivery.status = DeliveryStatus.SENT
            delivery.sent_at = datetime.now(UTC)
            delivery.error_summary = None
            await session.commit()

    async def _supplier(self, supplier_id: str) -> Supplier:
        try:
            supplier_uuid = UUID(supplier_id)
        except ValueError as error:
            raise SupplierMessagingError("Supplier ID is invalid") from error
        async with self._session_factory() as session:
            supplier = await session.get(Supplier, supplier_uuid)
            if supplier is None:
                raise SupplierMessagingError("Supplier was not found")
            if not supplier.is_active:
                raise SupplierMessagingError("Supplier is inactive")
            if not supplier.whatsapp_number:
                raise SupplierMessagingError("Supplier has no WhatsApp number")
            return supplier


def build_messaging_provider(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> WorkflowMessagingProvider:
    if settings.messaging_provider == "whatsapp_cloud":
        cloud_api = WhatsAppCloudApiClient(settings)
        transport: MessagingProvider = WhatsAppMessagingProvider(cloud_api)
    elif settings.messaging_provider == "mock":
        transport = MockMessagingProvider()
    else:
        raise ValueError(f"Unsupported messaging provider: {settings.messaging_provider}")
    return WorkflowMessagingProvider(session_factory, transport)


def _format_rfq(requirement: Requirement) -> str:
    lines = ["New request for quotation from KYood:"]
    if requirement.category is not None:
        lines.append(f"Category: {requirement.category.value}")
    if requirement.subcategory is not None:
        lines.append(f"Type: {requirement.subcategory}")
    if requirement.fabric is not None:
        lines.append(f"Fabric: {requirement.fabric}")
    if requirement.quantity is not None:
        lines.append(f"Quantity: {requirement.quantity} pieces")
    if requirement.target_price_min is not None or requirement.target_price_max is not None:
        currency = requirement.currency or "Currency not specified"
        lower = requirement.target_price_min or requirement.target_price_max
        upper = requirement.target_price_max or requirement.target_price_min
        lines.append(f"Target: {currency} {lower}–{upper} per piece")
    if requirement.delivery_city is not None:
        destination = requirement.delivery_city
        if requirement.delivery_state:
            destination = f"{destination}, {requirement.delivery_state}"
        lines.append(f"Delivery destination: {destination}")
    lines.append("Please reply with your current price, MOQ, stock, GST, and dispatch terms.")
    return "\n".join(lines)