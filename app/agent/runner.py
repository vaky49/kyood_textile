from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from langgraph.types import Command
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.decision_graph import build_decision_graph
from app.agent.extraction import RequirementExtractor, RuleBasedRequirementExtractor
from app.agent.graph import (
    SupplierMessagingProvider,
    build_procurement_graph,
)
from app.domain.buyer_decisions import BuyerAction, parse_buyer_intent
from app.domain.matching import SupplierMatch, SupplierOffer, rank_supplier_offers
from app.domain.models import (
    RFQ,
    Approval,
    Buyer,
    Catalogue,
    CatalogueItem,
    Conversation,
    Job,
    Message,
    Negotiation,
    ProcurementRequest,
    Quote,
    RequirementVersion,
    RFQSupplier,
    Supplier,
    SupplierResponse,
)
from app.domain.quotes import ParsedSupplierQuote, RuleBasedSupplierQuoteParser, SupplierQuoteParser
from app.domain.requirements import Requirement
from app.domain.statuses import (
    ApprovalStatus,
    ApprovalType,
    CatalogueStatus,
    JobStatus,
    MessageDirection,
    MessageType,
    NegotiationStatus,
    RequestStatus,
    RFQStatus,
    RFQSupplierStatus,
    SenderType,
    SupplierResponseStatus,
)
from app.infrastructure.db.jobs import JobLease, enqueue_job


class PermanentJobError(RuntimeError):
    pass


class BuyerMessageProvider(Protocol):
    async def send_message(self, phone_number: str, text: str, idempotency_key: str) -> None: ...


class MediaLoader(Protocol):
    async def load_for_vision(
        self,
        media_id: str,
        existing_object_key: str | None = None,
    ) -> tuple[str, str]: ...


class DatabaseSupplierSearcher:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def search(self, requirement: Requirement) -> list[SupplierMatch]:
        async with self._session_factory() as session:
            statement = (
                select(CatalogueItem, Supplier, Catalogue)
                .join(Supplier, Supplier.id == CatalogueItem.supplier_id)
                .join(Catalogue, Catalogue.id == CatalogueItem.catalogue_id)
                .where(
                    Supplier.is_active.is_(True),
                    Catalogue.status == CatalogueStatus.ACTIVE,
                )
            )
            if requirement.category is not None:
                statement = statement.where(CatalogueItem.category == requirement.category)
            if requirement.fabric is not None:
                statement = statement.where(
                    func.lower(CatalogueItem.fabric) == requirement.fabric.casefold()
                )
            rows = (await session.execute(statement)).all()

        offers = [
            SupplierOffer(
                supplier_id=str(supplier.id),
                catalogue_item_id=str(item.id),
                category=item.category,
                fabric=item.fabric,
                price=item.price,
                currency=item.currency,
                moq=item.moq,
                supplier_city=supplier.city,
                is_active=supplier.is_active,
                is_verified=supplier.is_verified,
                catalogue_age_days=_catalogue_age_days(
                    item.last_verified_at or catalogue.last_verified_at
                ),
                response_rate=(
                    float(supplier.response_rate)
                    if supplier.response_rate is not None
                    else None
                ),
            )
            for item, supplier, catalogue in rows
        ]
        return rank_supplier_offers(requirement, offers)


class MockSupplierMessaging:
    async def send_rfq(
        self,
        supplier_id: str,
        requirement: Requirement,
        idempotency_key: str,
    ) -> None:
        return None

    async def send_negotiation(
        self,
        supplier_id: str,
        target_price: str,
        currency: str,
        idempotency_key: str,
    ) -> None:
        return None


class MockBuyerMessaging:
    async def send_message(self, phone_number: str, text: str, idempotency_key: str) -> None:
        return None


class ProcurementJobProcessor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        checkpointer_factory: Any,
        extractor: RequirementExtractor | None = None,
        messaging: SupplierMessagingProvider | None = None,
        quote_parser: SupplierQuoteParser | None = None,
        buyer_messaging: BuyerMessageProvider | None = None,
        media_loader: MediaLoader | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._checkpointer_factory = checkpointer_factory
        self._extractor = extractor or RuleBasedRequirementExtractor()
        self._messaging = messaging or MockSupplierMessaging()
        self._quote_parser = quote_parser or RuleBasedSupplierQuoteParser()
        self._buyer_messaging = buyer_messaging or MockBuyerMessaging()
        self._media_loader = media_loader
        self._searcher = DatabaseSupplierSearcher(session_factory)

    async def process(self, job: JobLease) -> None:
        if job.job_type == "process_buyer_message":
            await self._process_buyer_message(job.payload)
            return
        if job.job_type == "decision_workflow":
            await self._run_decision_workflow(job.payload)
            return
        if job.job_type == "parse_supplier_response":
            await self._parse_supplier_response(job.payload)
            return
        if job.job_type == "finalize_rfq":
            await self._finalize_rfq(job.payload)
            return
        if job.job_type not in {"process_request", "resume_graph"}:
            raise PermanentJobError(f"Unsupported job type: {job.job_type}")

        try:
            request_id = UUID(job.payload["request_id"])
        except (KeyError, ValueError) as error:
            raise PermanentJobError("Job payload has no valid request_id") from error

        if job.job_type == "process_request":
            initial_state = await self._initial_state(request_id)
            resume_value = None
            thread_id = await self._thread_id(request_id)
            resume_approval_type = None
        else:
            initial_state = None
            resume_value, thread_id, resume_approval_type = await self._resume_value(
                job.payload,
                request_id,
            )
        config = {"configurable": {"thread_id": thread_id}}

        async with self._checkpointer_factory() as checkpointer:
            if resume_approval_type in {ApprovalType.NEGOTIATION, ApprovalType.ORDER_CONFIRMATION}:
                graph = build_decision_graph(
                    messaging=self._messaging,
                    checkpointer=checkpointer,
                )
                result = await graph.ainvoke(resume_value, config)
            else:
                graph = build_procurement_graph(
                    extractor=self._extractor,
                    searcher=self._searcher,
                    messaging=self._messaging,
                    checkpointer=checkpointer,
                )
                if initial_state is not None:
                    result = await graph.ainvoke(initial_state, config)
                else:
                    result = await graph.ainvoke(resume_value, config)

        async with self._session_factory() as session:
            interruptions = result.get("__interrupt__", ())
            if interruptions:
                interrupt_payload = interruptions[0].value
                if interrupt_payload.get("action") in {
                    "approve_negotiation",
                    "approve_order_confirmation",
                }:
                    await self._persist_decision_interrupt(
                        session,
                        request_id=request_id,
                        thread_id=thread_id,
                        interrupt_payload=interrupt_payload,
                    )
                else:
                    await self._persist_requirement(session, request_id, result)
                    await self._persist_interrupt(
                        session,
                        request_id=request_id,
                        thread_id=thread_id,
                        interrupt_payload=interrupt_payload,
                        requirement_version_id=result.get("requirement_version_id", "v1"),
                    )
            elif resume_approval_type in {
                ApprovalType.NEGOTIATION,
                ApprovalType.ORDER_CONFIRMATION,
            }:
                await self._persist_decision_completion(session, request_id, result)
            else:
                await self._persist_requirement(session, request_id, result)
                await self._persist_completion(session, request_id, result)

    async def _run_decision_workflow(self, payload: dict[str, Any]) -> None:
        try:
            request_id = UUID(payload["request_id"])
            message_id = UUID(payload["message_id"])
            supplier_id = str(payload["supplier_id"])
            action = str(payload["action"])
        except (KeyError, ValueError) as error:
            raise PermanentJobError("Decision workflow payload is incomplete") from error

        state: dict[str, Any] = {
            "request_id": str(request_id),
            "action": action,
            "supplier_id": supplier_id,
        }
        negotiation_id_value = payload.get("negotiation_id")
        if negotiation_id_value is not None:
            state["negotiation_id"] = str(negotiation_id_value)
            state["target_price"] = str(payload["target_price"])
            state["currency"] = str(payload["currency"])
        decision_key = str(negotiation_id_value or message_id)
        thread_id = f"decision:{request_id}:{decision_key}"
        config = {"configurable": {"thread_id": thread_id}}

        async with self._checkpointer_factory() as checkpointer:
            graph = build_decision_graph(
                messaging=self._messaging,
                checkpointer=checkpointer,
            )
            result = await graph.ainvoke(state, config)

        async with self._session_factory() as session:
            interruptions = result.get("__interrupt__", ())
            if interruptions:
                await self._persist_decision_interrupt(
                    session,
                    request_id=request_id,
                    thread_id=thread_id,
                    interrupt_payload=interruptions[0].value,
                )
            else:
                await self._persist_decision_completion(session, request_id, result)

    async def _persist_decision_interrupt(
        self,
        session: AsyncSession,
        *,
        request_id: UUID,
        thread_id: str,
        interrupt_payload: dict[str, Any],
    ) -> None:
        action = interrupt_payload.get("action")
        approval_type = {
            "approve_negotiation": ApprovalType.NEGOTIATION,
            "approve_order_confirmation": ApprovalType.ORDER_CONFIRMATION,
        }.get(action)
        if approval_type is None:
            raise PermanentJobError(f"Unsupported decision approval interrupt: {action}")
        interrupt_key = f"{thread_id}:{action}"
        approval = await session.scalar(
            select(Approval).where(Approval.interrupt_key == interrupt_key)
        )
        if approval is None:
            session.add(
                Approval(
                    request_id=request_id,
                    approval_type=approval_type,
                    thread_id=thread_id,
                    interrupt_key=interrupt_key,
                    payload=interrupt_payload,
                )
            )
        request = await session.get(ProcurementRequest, request_id)
        if request is not None:
            request.status = (
                RequestStatus.NEGOTIATING
                if approval_type is ApprovalType.NEGOTIATION
                else RequestStatus.AWAITING_ORDER_APPROVAL
            )
        if approval_type is ApprovalType.NEGOTIATION:
            negotiation = await session.get(Negotiation, UUID(interrupt_payload["negotiation_id"]))
            if negotiation is not None:
                negotiation.status = NegotiationStatus.AWAITING_APPROVAL
        await session.commit()

    async def _run_decision_workflow(self, payload: dict[str, Any]) -> None:
        try:
            request_id = UUID(payload["request_id"])
            message_id = UUID(payload["message_id"])
            action = payload["action"]
            supplier_id = payload["supplier_id"]
        except (KeyError, ValueError) as error:
            raise PermanentJobError("Decision workflow payload is incomplete") from error

        state: dict[str, Any] = {
            "request_id": str(request_id),
            "action": action,
            "supplier_id": str(supplier_id),
        }
        if payload.get("negotiation_id") is not None:
            state["negotiation_id"] = str(payload["negotiation_id"])
            state["target_price"] = str(payload["target_price"])
            state["currency"] = str(payload["currency"])
        decision_key = str(payload.get("negotiation_id", message_id))
        thread_id = f"decision:{request_id}:{decision_key}"
        config = {"configurable": {"thread_id": thread_id}}

        async with self._checkpointer_factory() as checkpointer:
            graph = build_decision_graph(
                messaging=self._messaging,
                checkpointer=checkpointer,
            )
            result = await graph.ainvoke(state, config)

        async with self._session_factory() as session:
            interruptions = result.get("__interrupt__", ())
            if interruptions:
                await self._persist_decision_interrupt(
                    session,
                    request_id=request_id,
                    thread_id=thread_id,
                    interrupt_payload=interruptions[0].value,
                )
            else:
                await self._persist_decision_completion(session, request_id, result)

    async def _persist_decision_completion(
        self,
        session: AsyncSession,
        request_id: UUID,
        result: dict[str, Any],
    ) -> None:
        request = await session.get(ProcurementRequest, request_id)
        if request is None:
            raise PermanentJobError("Decision request not found")
        try:
            request.status = RequestStatus(result["status"])
        except (KeyError, ValueError) as error:
            raise PermanentJobError("Decision graph returned an invalid status") from error

        negotiation_id = result.get("negotiation_id")
        if negotiation_id is not None:
            negotiation = await session.get(Negotiation, UUID(negotiation_id))
            if negotiation is None:
                raise PermanentJobError("Decision negotiation not found")
            if request.status is RequestStatus.NEGOTIATING:
                negotiation.status = NegotiationStatus.SENT
                await self._record_negotiation_message(session, negotiation)
            else:
                negotiation.status = NegotiationStatus.REJECTED

        if request.status is RequestStatus.ORDER_CONFIRMED:
            buyer = await session.get(Buyer, request.buyer_id)
            supplier_id = result.get("supplier_id")
            supplier = await session.get(Supplier, UUID(supplier_id)) if supplier_id else None
            supplier_name = supplier.business_name if supplier is not None else "the selected supplier"
            if buyer is not None:
                await self._send_buyer_text(
                    session,
                    request,
                    buyer,
                    f"An operator approved moving forward with {supplier_name}. This is not a purchase order; an operator will coordinate the next confirmation.",
                    f"order-approved:{request.id}",
                )
        await session.commit()

    async def _record_negotiation_message(
        self,
        session: AsyncSession,
        negotiation: Negotiation,
    ) -> None:
        external_message_id = f"negotiation:{negotiation.id}"
        if await session.scalar(
            select(Message.id).where(Message.external_message_id == external_message_id)
        ) is not None:
            return
        rfq_supplier = await session.get(RFQSupplier, negotiation.rfq_supplier_id)
        supplier = (
            await session.get(Supplier, rfq_supplier.supplier_id)
            if rfq_supplier is not None
            else None
        )
        if rfq_supplier is None or supplier is None:
            raise PermanentJobError("Negotiation supplier record not found")
        recipient = supplier.whatsapp_number or str(supplier.id)
        conversation = await session.scalar(
            select(Conversation).where(
                Conversation.supplier_id == supplier.id,
                Conversation.channel == "whatsapp_supplier",
            )
        )
        if conversation is None:
            conversation = Conversation(
                supplier_id=supplier.id,
                channel="whatsapp_supplier",
                external_conversation_id=recipient,
            )
            session.add(conversation)
            await session.flush()
        text = (
            f"Can you offer {negotiation.target_currency} {negotiation.target_price} "
            "for the requested quantity? Please confirm your final price and GST terms."
        )
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender_type=SenderType.SYSTEM,
                message_type=MessageType.TEXT,
                text=text,
                external_message_id=external_message_id,
            )
        )

    async def _initial_state(self, request_id: UUID) -> dict[str, Any]:
        async with self._session_factory() as session:
            request = await session.get(ProcurementRequest, request_id)
            if request is None:
                raise PermanentJobError("Procurement request not found")
            buyer = await session.get(Buyer, request.buyer_id)
            if buyer is None:
                raise PermanentJobError("Request buyer not found")
            source_message = (
                await session.get(Message, request.source_message_id)
                if request.source_message_id is not None
                else None
            )
            media_id = source_message.external_media_id if source_message is not None else None
            media_url = source_message.media_url if source_message is not None else None
            media_object_key = source_message.media_object_key if source_message is not None else None
            version_id = str(uuid5(NAMESPACE_URL, f"requirement:{request_id}:1"))
            initial_state: dict[str, Any] = {
                "request_id": str(request.id),
                "buyer_id": str(request.buyer_id),
                "requirement_version_id": version_id,
                "raw_message": request.raw_message or "",
                "media_urls": [],
                "buyer_city": buyer.city,
                "buyer_state": buyer.state,
                "status": RequestStatus.NEW.value,
            }
        if media_id is not None and self._media_loader is not None:
            object_key, vision_data_url = await self._media_loader.load_for_vision(
                media_id,
                media_object_key,
            )
            async with self._session_factory() as session:
                source_message = await session.get(Message, request.source_message_id)
                if source_message is not None:
                    source_message.media_object_key = object_key
                    await session.commit()
            initial_state["media_urls"] = [vision_data_url]
        elif media_url and media_url.startswith(("https://", "http://", "data:")):
            initial_state["media_urls"] = [media_url]
        return initial_state

    async def _thread_id(self, request_id: UUID) -> str:
        version_id = str(uuid5(NAMESPACE_URL, f"requirement:{request_id}:1"))
        return f"{request_id}:{version_id}"

    async def _process_buyer_message(self, payload: dict[str, Any]) -> None:
        try:
            request_id = UUID(payload["request_id"])
            message_id = UUID(payload["message_id"])
        except (KeyError, ValueError) as error:
            raise PermanentJobError("Buyer message job is missing request or message ID") from error

        async with self._session_factory() as session:
            request = await session.get(ProcurementRequest, request_id)
            message = await session.get(Message, message_id)
            if request is None or message is None:
                raise PermanentJobError("Buyer request or message not found")
            buyer = await session.get(Buyer, request.buyer_id)
            if buyer is None or request.comparison_payload is None:
                raise PermanentJobError("Buyer or quote comparison not found")

            intent = parse_buyer_intent(message.text or "")
            quotes = request.comparison_payload.get("quotes", [])
            selected_quote: dict[str, Any] | None = None
            if intent.selection_key is not None:
                selected_quote = next(
                    (
                        quote
                        for quote in quotes
                        if quote.get("selection_key", "").casefold()
                        == intent.selection_key.casefold()
                    ),
                    None,
                )

            if intent.action is BuyerAction.SELECT:
                if selected_quote is None:
                    await self._send_buyer_text(
                        session,
                        request,
                        buyer,
                        "I couldn't match that supplier choice. Reply with one of the supplier letters shown in your quotes.",
                        f"decision-help:{message.id}",
                    )
                    await session.commit()
                    return
                request.buyer_decision = {
                    "action": "select",
                    "supplier_id": selected_quote["supplier_id"],
                    "quote_id": selected_quote["quote_id"],
                    "selection_key": selected_quote["selection_key"],
                }
                request.status = RequestStatus.AWAITING_ORDER_APPROVAL
                await self._enqueue_decision_workflow(
                    session,
                    request_id=request.id,
                    message_id=message.id,
                    action="order_confirmation",
                    supplier_id=selected_quote["supplier_id"],
                )
                await session.commit()
                return

            if intent.action is BuyerAction.CONFIRM:
                supplier_id = (
                    selected_quote["supplier_id"]
                    if selected_quote is not None
                    else (request.buyer_decision or {}).get("supplier_id")
                )
                if supplier_id is None:
                    await self._send_buyer_text(
                        session,
                        request,
                        buyer,
                        "Choose a supplier from the quotes before confirming.",
                        f"decision-help:{message.id}",
                    )
                    await session.commit()
                    return
                request.buyer_decision = {
                    **(request.buyer_decision or {}),
                    "action": "confirm",
                    "supplier_id": supplier_id,
                }
                request.status = RequestStatus.AWAITING_ORDER_APPROVAL
                await self._enqueue_decision_workflow(
                    session,
                    request_id=request.id,
                    message_id=message.id,
                    action="order_confirmation",
                    supplier_id=supplier_id,
                )
                await session.commit()
                return

            if intent.action is BuyerAction.NEGOTIATE:
                if selected_quote is None or intent.target_price is None:
                    await self._send_buyer_text(
                        session,
                        request,
                        buyer,
                        "I couldn't match that negotiation request. Reply like ‘Negotiate A at ₹490’ using a supplier letter from the quotes.",
                        f"decision-help:{message.id}",
                    )
                    await session.commit()
                    return
                currency = intent.currency or selected_quote.get("currency")
                if currency is None:
                    await self._send_buyer_text(
                        session,
                        request,
                        buyer,
                        "The supplier didn't specify a currency. Which currency should I use for your target price?",
                        f"decision-currency:{message.id}",
                    )
                    await session.commit()
                    return
                rfq_id = UUID(request.comparison_payload["rfq_id"])
                rfq_supplier = await session.scalar(
                    select(RFQSupplier).where(
                        RFQSupplier.rfq_id == rfq_id,
                        RFQSupplier.supplier_id == UUID(selected_quote["supplier_id"]),
                    )
                )
                if rfq_supplier is None:
                    raise PermanentJobError("Selected supplier is not part of the comparison RFQ")
                negotiation = Negotiation(
                    request_id=request.id,
                    rfq_supplier_id=rfq_supplier.id,
                    buyer_message_id=message.id,
                    target_price=intent.target_price,
                    target_currency=currency,
                    idempotency_key=f"negotiation:{message.id}",
                )
                session.add(negotiation)
                await session.flush()
                request.buyer_decision = {
                    "action": "negotiate",
                    "supplier_id": selected_quote["supplier_id"],
                    "quote_id": selected_quote["quote_id"],
                    "selection_key": selected_quote["selection_key"],
                    "negotiation_id": str(negotiation.id),
                }
                request.status = RequestStatus.NEGOTIATING
                await self._enqueue_decision_workflow(
                    session,
                    request_id=request.id,
                    message_id=message.id,
                    action="negotiate",
                    supplier_id=selected_quote["supplier_id"],
                    negotiation_id=negotiation.id,
                    target_price=str(intent.target_price),
                    currency=currency,
                )
                await session.commit()
                return

            await self._send_buyer_text(
                session,
                request,
                buyer,
                "Reply with a supplier letter to select it, or ‘Negotiate A at ₹490’ to request a target price.",
                f"decision-help:{message.id}",
            )
            await session.commit()

    async def _enqueue_decision_workflow(
        self,
        session: AsyncSession,
        *,
        request_id: UUID,
        message_id: UUID,
        action: str,
        supplier_id: str,
        negotiation_id: UUID | None = None,
        target_price: str | None = None,
        currency: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "request_id": str(request_id),
            "message_id": str(message_id),
            "action": action,
            "supplier_id": supplier_id,
        }
        if negotiation_id is not None:
            payload["negotiation_id"] = str(negotiation_id)
        if target_price is not None:
            payload["target_price"] = target_price
        if currency is not None:
            payload["currency"] = currency
        await enqueue_job(
            session,
            job_type="decision_workflow",
            payload=payload,
            idempotency_key=f"decision-workflow:{message_id}",
        )

    async def _send_buyer_text(
        self,
        session: AsyncSession,
        request: ProcurementRequest,
        buyer: Buyer,
        text: str,
        external_message_id: str,
    ) -> None:
        if await session.scalar(
            select(Message.id).where(Message.external_message_id == external_message_id)
        ) is not None:
            return
        if not buyer.whatsapp_number:
            return
        await self._buyer_messaging.send_message(
            buyer.whatsapp_number,
            text,
            idempotency_key=external_message_id,
        )
        conversation_id = request.conversation_id
        if conversation_id is None:
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.buyer_id == buyer.id,
                    Conversation.channel == "whatsapp",
                )
            )
            if conversation is None:
                conversation = Conversation(
                    buyer_id=buyer.id,
                    channel="whatsapp",
                    external_conversation_id=buyer.whatsapp_number,
                )
                session.add(conversation)
                await session.flush()
            conversation_id = conversation.id
        session.add(
            Message(
                conversation_id=conversation_id,
                direction=MessageDirection.OUTBOUND,
                sender_type=SenderType.SYSTEM,
                message_type=MessageType.TEXT,
                text=text,
                external_message_id=external_message_id,
            )
        )

    async def _resume_value(
        self,
        payload: dict[str, Any],
        request_id: UUID,
    ) -> tuple[Command, str, ApprovalType]:
        try:
            approval_id = UUID(payload["approval_id"])
        except (KeyError, ValueError) as error:
            raise PermanentJobError("Resume job payload has no valid approval_id") from error
        async with self._session_factory() as session:
            approval = await session.get(Approval, approval_id)
            if approval is None or approval.decision_payload is None:
                raise PermanentJobError("Resume approval or decision payload not found")
            if approval.request_id != request_id:
                raise PermanentJobError("Resume approval belongs to another request")
            if approval.status not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
                raise PermanentJobError("Cannot resume a graph from a pending approval")
            return (
                Command(resume=approval.decision_payload),
                approval.thread_id,
                approval.approval_type,
            )

    async def _persist_requirement(
        self,
        session: AsyncSession,
        request_id: UUID,
        result: dict[str, Any],
    ) -> None:
        requirement_payload = result.get("requirement")
        if requirement_payload is None:
            return
        existing = await session.scalar(
            select(RequirementVersion).where(
                RequirementVersion.request_id == request_id,
                RequirementVersion.version == 1,
            )
        )
        if existing is None:
            requirement = Requirement.model_validate(requirement_payload)
            session.add(
                RequirementVersion(
                    request_id=request_id,
                    version=1,
                    payload=requirement.model_dump(mode="json"),
                    fact_provenance={
                        key: value.model_dump(mode="json")
                        for key, value in requirement.fact_provenance.items()
                    },
                )
            )

    async def _persist_interrupt(
        self,
        session: AsyncSession,
        *,
        request_id: UUID,
        thread_id: str,
        interrupt_payload: dict[str, Any],
        requirement_version_id: str,
    ) -> None:
        action = interrupt_payload.get("action")
        approval_type = {
            "select_suppliers": ApprovalType.SUPPLIER_SELECTION,
            "approve_rfq": ApprovalType.RFQ,
        }.get(action)
        if approval_type is None:
            raise PermanentJobError(f"Unsupported graph approval interrupt: {action}")

        interrupt_key = f"{thread_id}:{action}"
        approval = await session.scalar(
            select(Approval).where(Approval.interrupt_key == interrupt_key)
        )
        if approval is None:
            approval = Approval(
                request_id=request_id,
                approval_type=approval_type,
                thread_id=thread_id,
                interrupt_key=interrupt_key,
                payload=interrupt_payload,
            )
            session.add(approval)

        procurement_request = await session.get(ProcurementRequest, request_id)
        if procurement_request is not None:
            procurement_request.status = (
                RequestStatus.AWAITING_SUPPLIER_SELECTION
                if approval_type is ApprovalType.SUPPLIER_SELECTION
                else RequestStatus.AWAITING_RFQ_APPROVAL
            )

        if approval_type is ApprovalType.RFQ:
            await self._ensure_rfq(session, request_id, interrupt_payload, requirement_version_id)
        await session.commit()

    async def _ensure_rfq(
        self,
        session: AsyncSession,
        request_id: UUID,
        interrupt_payload: dict[str, Any],
        requirement_version_id: str,
    ) -> None:
        rfq_id = uuid5(NAMESPACE_URL, f"rfq:{request_id}:{requirement_version_id}")
        rfq = await session.get(RFQ, rfq_id)
        if rfq is None:
            request = await session.get(ProcurementRequest, request_id)
            if request is None:
                raise PermanentJobError("RFQ request no longer exists")
            rfq = RFQ(
                id=rfq_id,
                request_id=request_id,
                buyer_id=request.buyer_id,
                status=RFQStatus.AWAITING_APPROVAL,
                requirement_snapshot=interrupt_payload["requirement"],
                idempotency_key=f"rfq:{request_id}:{requirement_version_id}",
            )
            session.add(rfq)

        for supplier_id in interrupt_payload["selected_supplier_ids"]:
            supplier_uuid = UUID(supplier_id)
            idempotency_key = f"rfq:{rfq_id}:supplier:{supplier_id}"
            existing = await session.scalar(
                select(RFQSupplier).where(RFQSupplier.idempotency_key == idempotency_key)
            )
            if existing is None:
                session.add(
                    RFQSupplier(
                        rfq_id=rfq_id,
                        supplier_id=supplier_uuid,
                        status=RFQSupplierStatus.PENDING,
                        idempotency_key=idempotency_key,
                    )
                )

    async def _persist_completion(
        self,
        session: AsyncSession,
        request_id: UUID,
        result: dict[str, Any],
    ) -> None:
        procurement_request = await session.get(ProcurementRequest, request_id)
        if procurement_request is None:
            return
        try:
            procurement_request.status = RequestStatus(result["status"])
        except (KeyError, ValueError):
            procurement_request.status = RequestStatus.FAILED

        rfq_id_value = result.get("rfq_id")
        if rfq_id_value is not None:
            rfq = await session.get(RFQ, UUID(rfq_id_value))
            if rfq is not None:
                sent_ids = set(result.get("sent_supplier_ids", []))
                failed_ids = set(result.get("failed_supplier_ids", []))
                rfq.status = RFQStatus.WAITING_FOR_QUOTES if sent_ids else RFQStatus.FAILED
                rfq.sent_at = datetime.now(UTC) if sent_ids else None
                rfq.deadline = rfq.sent_at + timedelta(hours=4) if rfq.sent_at else None
                suppliers = await session.scalars(
                    select(RFQSupplier).where(RFQSupplier.rfq_id == rfq.id)
                )
                for rfq_supplier in suppliers:
                    if str(rfq_supplier.supplier_id) in sent_ids:
                        rfq_supplier.status = RFQSupplierStatus.SENT
                        rfq_supplier.sent_at = rfq.sent_at
                    elif str(rfq_supplier.supplier_id) in failed_ids:
                        rfq_supplier.status = RFQSupplierStatus.FAILED
                if sent_ids and rfq.deadline is not None:
                    await enqueue_job(
                        session,
                        job_type="finalize_rfq",
                        payload={"rfq_id": str(rfq.id)},
                        idempotency_key=f"finalize-rfq:{rfq.id}",
                        run_after=rfq.deadline,
                    )
        await session.commit()

    async def _parse_supplier_response(self, payload: dict[str, Any]) -> None:
        try:
            response_id = UUID(payload["supplier_response_id"])
        except (KeyError, ValueError) as error:
            raise PermanentJobError("Response job payload has no valid supplier_response_id") from error

        async with self._session_factory() as session:
            response = await session.get(SupplierResponse, response_id)
            if response is None:
                raise PermanentJobError("Supplier response not found")
            if response.rfq_supplier_id is None:
                response.status = SupplierResponseStatus.NEEDS_REVIEW
                await session.commit()
                return

            negotiation = await session.scalar(
                select(Negotiation).where(
                    Negotiation.response_message_id == response.source_message_id
                )
            )
            if negotiation is not None:
                await self._parse_negotiation_response(session, response, negotiation)
                return

            existing_quote = await session.scalar(
                select(Quote).where(Quote.supplier_response_id == response.id)
            )
            if existing_quote is not None:
                return

            parsed: ParsedSupplierQuote = self._quote_parser.parse(response.raw_text or "")
            response.parsed_payload = parsed.model_dump(mode="json")
            response.extraction_confidence = Decimal(str(parsed.confidence))
            response.status = (
                SupplierResponseStatus.NEEDS_REVIEW
                if parsed.needs_human_review
                else SupplierResponseStatus.PARSED
            )
            session.add(
                Quote(
                    supplier_response_id=response.id,
                    supplier_id=response.supplier_id,
                    price=parsed.price,
                    currency=parsed.currency,
                    price_unit=parsed.price_unit,
                    gst_included=parsed.gst_included,
                    moq=parsed.moq,
                    available_quantity=parsed.available_quantity,
                    dispatch_days=parsed.dispatch_days,
                    payment_terms=parsed.payment_terms,
                    shipping_terms=parsed.shipping_terms,
                    extraction_confidence=Decimal(str(parsed.confidence)),
                    needs_human_review=parsed.needs_human_review,
                )
            )
            rfq_supplier = await session.get(RFQSupplier, response.rfq_supplier_id)
            if rfq_supplier is not None:
                rfq_supplier.status = RFQSupplierStatus.RESPONDED
                rfq_supplier.responded_at = datetime.now(UTC)
                await self._expedite_finalizer(session, rfq_supplier.rfq_id)
            await session.commit()

    async def _parse_negotiation_response(
        self,
        session: AsyncSession,
        response: SupplierResponse,
        negotiation: Negotiation,
    ) -> None:
        parsed = self._quote_parser.parse(response.raw_text or "")
        response.parsed_payload = parsed.model_dump(mode="json")
        response.extraction_confidence = Decimal(str(parsed.confidence))
        response.status = (
            SupplierResponseStatus.NEEDS_REVIEW
            if parsed.needs_human_review
            else SupplierResponseStatus.PARSED
        )
        negotiation.supplier_price = parsed.price
        negotiation.supplier_currency = parsed.currency
        negotiation.supplier_gst_included = parsed.gst_included
        negotiation.supplier_response_text = response.raw_text
        negotiation.responded_at = datetime.now(UTC)
        if parsed.price is None or parsed.currency is None:
            negotiation.status = NegotiationStatus.NEEDS_REVIEW
            await session.commit()
            return

        negotiation.status = NegotiationStatus.RESPONDED
        request = await session.get(ProcurementRequest, negotiation.request_id)
        if request is not None:
            request.status = RequestStatus.AWAITING_BUYER_DECISION
            buyer = await session.get(Buyer, request.buyer_id)
            supplier = await session.get(Supplier, response.supplier_id)
            if buyer is not None:
                gst_text = (
                    "GST included"
                    if parsed.gst_included is True
                    else "GST extra"
                    if parsed.gst_included is False
                    else "GST status not specified"
                )
                supplier_name = supplier.business_name if supplier is not None else "The supplier"
                await self._send_buyer_text(
                    session,
                    request,
                    buyer,
                    f"{supplier_name} replied with {parsed.currency} {parsed.price}. {gst_text}. Reply ‘Confirm’ to request operator approval, or ask to negotiate again.",
                    f"negotiation-result:{negotiation.id}",
                )
        await session.commit()

    async def _expedite_finalizer(self, session: AsyncSession, rfq_id: UUID) -> None:
        outstanding = await session.scalar(
            select(func.count())
            .select_from(RFQSupplier)
            .where(
                RFQSupplier.rfq_id == rfq_id,
                RFQSupplier.status == RFQSupplierStatus.SENT,
            )
        )
        if outstanding:
            return
        job = await session.scalar(
            select(Job).where(Job.idempotency_key == f"finalize-rfq:{rfq_id}")
        )
        if job is not None and job.status in {JobStatus.QUEUED, JobStatus.RETRYABLE_FAILED}:
            job.status = JobStatus.QUEUED
            job.run_after = datetime.now(UTC)

    async def _finalize_rfq(self, payload: dict[str, Any]) -> None:
        try:
            rfq_id = UUID(payload["rfq_id"])
        except (KeyError, ValueError) as error:
            raise PermanentJobError("Finalization job payload has no valid rfq_id") from error

        async with self._session_factory() as session:
            rfq = await session.get(RFQ, rfq_id)
            if rfq is None:
                raise PermanentJobError("RFQ not found")
            procurement_request = await session.get(ProcurementRequest, rfq.request_id)
            if procurement_request is None:
                raise PermanentJobError("RFQ request not found")
            buyer = await session.get(Buyer, rfq.buyer_id)
            if buyer is None:
                raise PermanentJobError("RFQ buyer not found")

            rows = (
                await session.execute(
                    select(Quote, Supplier, SupplierResponse)
                    .join(SupplierResponse, SupplierResponse.id == Quote.supplier_response_id)
                    .join(RFQSupplier, RFQSupplier.id == SupplierResponse.rfq_supplier_id)
                    .join(Supplier, Supplier.id == Quote.supplier_id)
                    .where(RFQSupplier.rfq_id == rfq.id)
                )
            ).all()
            quotes = [_comparison_quote(quote, supplier, response) for quote, supplier, response in rows]
            quotes.sort(
                key=lambda quote: (
                    quote["price"] is None,
                    quote["currency"] is None,
                    quote["currency"] or "",
                    Decimal(quote["price"]) if quote["price"] is not None else Decimal(0),
                    quote["supplier"],
                )
            )
            for index, quote in enumerate(quotes):
                quote["selection_key"] = _selection_key(index)
            comparison = {
                "rfq_id": str(rfq.id),
                "quotes": quotes,
                "quote_count": len(quotes),
                "generated_at": datetime.now(UTC).isoformat(),
            }
            procurement_request.comparison_payload = comparison
            if quotes:
                rfq.status = RFQStatus.QUOTES_READY
                procurement_request.status = RequestStatus.AWAITING_BUYER_DECISION
            else:
                rfq.status = RFQStatus.EXPIRED
                procurement_request.status = RequestStatus.FAILED

            await self._send_buyer_comparison(
                session,
                procurement_request,
                buyer,
                rfq,
                _format_buyer_comparison(quotes),
            )
            await session.commit()

    async def _send_buyer_comparison(
        self,
        session: AsyncSession,
        procurement_request: ProcurementRequest,
        buyer: Buyer,
        rfq: RFQ,
        text: str,
    ) -> None:
        external_message_id = f"comparison:{rfq.id}"
        already_sent = await session.scalar(
            select(Message.id).where(Message.external_message_id == external_message_id)
        )
        if already_sent is not None or not buyer.whatsapp_number:
            return

        await self._buyer_messaging.send_message(
            buyer.whatsapp_number,
            text,
            idempotency_key=external_message_id,
        )
        conversation_id = procurement_request.conversation_id
        if conversation_id is None:
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.buyer_id == buyer.id,
                    Conversation.channel == "whatsapp",
                )
            )
            if conversation is None:
                conversation = Conversation(
                    buyer_id=buyer.id,
                    channel="whatsapp",
                    external_conversation_id=buyer.whatsapp_number,
                )
                session.add(conversation)
                await session.flush()
            conversation_id = conversation.id
        session.add(
            Message(
                conversation_id=conversation_id,
                direction=MessageDirection.OUTBOUND,
                sender_type=SenderType.SYSTEM,
                message_type=MessageType.TEXT,
                text=text,
                external_message_id=external_message_id,
            )
        )


def _comparison_quote(quote: Quote, supplier: Supplier, response: SupplierResponse) -> dict[str, Any]:
    return {
    "quote_id": str(quote.id),
        "supplier_id": str(supplier.id),
        "supplier": supplier.business_name,
        "price": str(quote.price) if quote.price is not None else None,
        "currency": quote.currency,
        "price_unit": quote.price_unit,
        "gst_included": quote.gst_included,
        "moq": quote.moq,
        "available_quantity": quote.available_quantity,
        "dispatch_days": quote.dispatch_days,
        "payment_terms": quote.payment_terms,
        "shipping_terms": quote.shipping_terms,
        "confidence": float(quote.extraction_confidence)
        if quote.extraction_confidence is not None
        else None,
        "needs_human_review": quote.needs_human_review,
        "source_message_id": str(response.source_message_id),
        "received_at": response.received_at.isoformat(),
    }


def _format_buyer_comparison(quotes: list[dict[str, Any]]) -> str:
    if not quotes:
        return "No suppliers sent a quote before the response deadline. I can send a new RFQ if you would like."
    lines = [f"I received {len(quotes)} quote{'s' if len(quotes) != 1 else ''}:"]
    for index, quote in enumerate(quotes, start=1):
        price = quote["price"]
        currency = quote["currency"]
        unit = quote["price_unit"]
        if price is None:
            price_text = "Price not specified"
        elif currency is None:
            price_text = f"{price} (currency not specified)"
        elif unit is None:
            price_text = f"{currency} {price}"
        else:
            price_text = f"{currency} {price}/{unit}"
        gst = (
            "GST included"
            if quote["gst_included"] is True
            else "GST extra"
            if quote["gst_included"] is False
            else "GST status not specified"
        )
        lines.extend(
            [
                f"{quote.get('selection_key', _selection_key(index - 1))}. {quote['supplier']}",
                f"{price_text}; {gst}",
                f"MOQ: {quote['moq'] if quote['moq'] is not None else 'not specified'}",
                f"Stock reported: {quote['available_quantity'] if quote['available_quantity'] is not None else 'not specified'}",
                f"Dispatch: {quote['dispatch_days']} days"
                if quote["dispatch_days"] is not None
                else "Dispatch: not specified",
            ]
        )
        if quote["needs_human_review"]:
            lines.append("Some quote details need operator confirmation.")
    lines.append("Reply with a supplier letter to select it, or ask to negotiate that letter.")
    return "\n".join(lines)


def _selection_key(index: int) -> str:
    if 0 <= index < 26:
        return chr(ord("A") + index)
    return str(index + 1)


def _catalogue_age_days(last_verified_at: datetime | None) -> int | None:
    if last_verified_at is None:
        return None
    if last_verified_at.tzinfo is None:
        last_verified_at = last_verified_at.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - last_verified_at).days)