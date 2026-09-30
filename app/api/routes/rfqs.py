from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_operator
from app.domain.models import RFQ, Message, Quote, RFQSupplier, Supplier, SupplierResponse
from app.domain.statuses import RFQStatus, RFQSupplierStatus
from app.infrastructure.db.session import get_session

router = APIRouter(prefix="/api/rfqs", tags=["rfqs"])
OperatorAccess = Annotated[None, Depends(require_operator)]
DatabaseSession = Annotated[AsyncSession, Depends(get_session)]


class RFQListItem(BaseModel):
    id: UUID
    request_id: UUID
    status: RFQStatus
    created_at: datetime
    sent_at: datetime | None
    deadline: datetime | None
    supplier_count: int


class QuoteSourceView(BaseModel):
    quote_id: UUID
    supplier_response_id: UUID
    supplier_id: UUID
    supplier_name: str
    price: Decimal | None
    currency: str | None
    price_unit: str | None
    gst_included: bool | None
    moq: int | None
    available_quantity: int | None
    dispatch_days: int | None
    payment_terms: str | None
    shipping_terms: str | None
    extraction_confidence: Decimal | None
    needs_human_review: bool
    source_message_id: UUID
    source_message_text: str | None
    received_at: datetime


class RFQSupplierView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    supplier_id: UUID
    supplier_name: str
    status: RFQSupplierStatus
    sent_at: datetime | None
    responded_at: datetime | None


class RFQDetail(BaseModel):
    id: UUID
    request_id: UUID
    buyer_id: UUID
    status: RFQStatus
    requirement_snapshot: dict
    created_at: datetime
    sent_at: datetime | None
    deadline: datetime | None
    suppliers: list[RFQSupplierView]
    quotes: list[QuoteSourceView]


@router.get("", response_model=list[RFQListItem])
async def list_rfqs(
    session: DatabaseSession,
    _operator: OperatorAccess,
    rfq_status: Annotated[RFQStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RFQListItem]:
    statement = select(RFQ).order_by(RFQ.created_at.desc(), RFQ.id).limit(limit).offset(offset)
    if rfq_status is not None:
        statement = statement.where(RFQ.status == rfq_status)
    rfqs = list(await session.scalars(statement))
    rfq_ids = [rfq.id for rfq in rfqs]
    supplier_counts = {}
    if rfq_ids:
        supplier_rows = await session.execute(
            select(RFQSupplier.rfq_id, RFQSupplier.id).where(RFQSupplier.rfq_id.in_(rfq_ids))
        )
        for rfq_id, _supplier_id in supplier_rows:
            supplier_counts[rfq_id] = supplier_counts.get(rfq_id, 0) + 1
    return [
        RFQListItem(
            id=rfq.id,
            request_id=rfq.request_id,
            status=rfq.status,
            created_at=rfq.created_at,
            sent_at=rfq.sent_at,
            deadline=rfq.deadline,
            supplier_count=supplier_counts.get(rfq.id, 0),
        )
        for rfq in rfqs
    ]


@router.get("/{rfq_id}", response_model=RFQDetail)
async def get_rfq(
    rfq_id: UUID,
    session: DatabaseSession,
    _operator: OperatorAccess,
) -> RFQDetail:
    rfq = await session.get(RFQ, rfq_id)
    if rfq is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RFQ not found")
    supplier_rows = (
        await session.execute(
            select(RFQSupplier, Supplier)
            .join(Supplier, Supplier.id == RFQSupplier.supplier_id)
            .where(RFQSupplier.rfq_id == rfq.id)
            .order_by(Supplier.business_name)
        )
    ).all()
    suppliers = [
        RFQSupplierView(
            id=rfq_supplier.id,
            supplier_id=supplier.id,
            supplier_name=supplier.business_name,
            status=rfq_supplier.status,
            sent_at=rfq_supplier.sent_at,
            responded_at=rfq_supplier.responded_at,
        )
        for rfq_supplier, supplier in supplier_rows
    ]
    quote_rows = (
        await session.execute(
            select(Quote, Supplier, SupplierResponse, Message)
            .join(SupplierResponse, SupplierResponse.id == Quote.supplier_response_id)
            .join(Message, Message.id == SupplierResponse.source_message_id)
            .join(Supplier, Supplier.id == Quote.supplier_id)
            .join(RFQSupplier, RFQSupplier.id == SupplierResponse.rfq_supplier_id)
            .where(RFQSupplier.rfq_id == rfq.id)
            .order_by(Quote.extracted_at, Supplier.business_name)
        )
    ).all()
    quotes = [
        QuoteSourceView(
            quote_id=quote.id,
            supplier_response_id=response.id,
            supplier_id=supplier.id,
            supplier_name=supplier.business_name,
            price=quote.price,
            currency=quote.currency,
            price_unit=quote.price_unit,
            gst_included=quote.gst_included,
            moq=quote.moq,
            available_quantity=quote.available_quantity,
            dispatch_days=quote.dispatch_days,
            payment_terms=quote.payment_terms,
            shipping_terms=quote.shipping_terms,
            extraction_confidence=quote.extraction_confidence,
            needs_human_review=quote.needs_human_review,
            source_message_id=message.id,
            source_message_text=message.text,
            received_at=response.received_at,
        )
        for quote, supplier, response, message in quote_rows
    ]
    return RFQDetail(
        id=rfq.id,
        request_id=rfq.request_id,
        buyer_id=rfq.buyer_id,
        status=rfq.status,
        requirement_snapshot=rfq.requirement_snapshot,
        created_at=rfq.created_at,
        sent_at=rfq.sent_at,
        deadline=rfq.deadline,
        suppliers=suppliers,
        quotes=quotes,
    )