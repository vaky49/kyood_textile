from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_operator
from app.domain.models import (
    RFQ,
    Approval,
    Buyer,
    Message,
    ProcurementRequest,
    RequirementVersion,
)
from app.domain.statuses import ApprovalStatus, ApprovalType, RequestStatus
from app.infrastructure.db.session import get_session

router = APIRouter(prefix="/api/requests", tags=["requests"])
OperatorAccess = Annotated[None, Depends(require_operator)]
DatabaseSession = Annotated[AsyncSession, Depends(get_session)]


class RequestListItem(BaseModel):
    id: UUID
    buyer_name: str | None
    buyer_business_name: str | None
    category: str | None
    quantity: int | None
    status: RequestStatus
    created_at: datetime
    updated_at: datetime


class MessageView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    direction: str
    sender_type: str
    message_type: str
    text: str | None
    media_url: str | None
    external_media_id: str | None
    media_object_key: str | None
    external_message_id: str | None
    created_at: datetime


class RequirementVersionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: int
    payload: dict[str, Any]
    fact_provenance: dict[str, Any]
    created_at: datetime


class ApprovalView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    approval_type: ApprovalType
    status: ApprovalStatus
    payload: dict[str, Any]
    created_at: datetime
    decided_at: datetime | None


class RequestDetail(BaseModel):
    id: UUID
    buyer_id: UUID
    buyer_name: str | None
    buyer_business_name: str | None
    buyer_phone: str | None
    raw_message: str | None
    status: RequestStatus
    comparison_payload: dict[str, Any] | None
    buyer_decision: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageView]
    requirement_versions: list[RequirementVersionView]
    approvals: list[ApprovalView]
    rfq_ids: list[UUID]


@router.get("", response_model=list[RequestListItem])
async def list_requests(
    session: DatabaseSession,
    _operator: OperatorAccess,
    request_status: Annotated[RequestStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RequestListItem]:
    statement = (
        select(ProcurementRequest, Buyer)
        .join(Buyer, Buyer.id == ProcurementRequest.buyer_id)
        .order_by(ProcurementRequest.created_at.desc(), ProcurementRequest.id)
        .limit(limit)
        .offset(offset)
    )
    if request_status is not None:
        statement = statement.where(ProcurementRequest.status == request_status)
    rows = (await session.execute(statement)).all()
    request_ids = [request.id for request, _buyer in rows]
    versions = await session.scalars(
        select(RequirementVersion)
        .where(RequirementVersion.request_id.in_(request_ids))
        .order_by(RequirementVersion.version.desc())
    ) if request_ids else []
    latest_versions: dict[UUID, dict[str, Any]] = {}
    for version in versions:
        latest_versions.setdefault(version.request_id, version.payload)

    return [
        RequestListItem(
            id=request.id,
            buyer_name=buyer.name,
            buyer_business_name=buyer.business_name,
            category=latest_versions.get(request.id, {}).get("category"),
            quantity=latest_versions.get(request.id, {}).get("quantity"),
            status=request.status,
            created_at=request.created_at,
            updated_at=request.updated_at,
        )
        for request, buyer in rows
    ]


@router.get("/{request_id}", response_model=RequestDetail)
async def get_request(
    request_id: UUID,
    session: DatabaseSession,
    _operator: OperatorAccess,
) -> RequestDetail:
    request = await session.get(ProcurementRequest, request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    buyer = await session.get(Buyer, request.buyer_id)
    if buyer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Buyer not found")

    messages: list[Message] = []
    if request.conversation_id is not None:
        messages = list(
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == request.conversation_id)
                .order_by(Message.created_at, Message.id)
            )
        )
    versions = list(
        await session.scalars(
            select(RequirementVersion)
            .where(RequirementVersion.request_id == request_id)
            .order_by(RequirementVersion.version)
        )
    )
    approvals = list(
        await session.scalars(
            select(Approval)
            .where(Approval.request_id == request_id)
            .order_by(Approval.created_at)
        )
    )
    rfq_ids = list(
        await session.scalars(select(RFQ.id).where(RFQ.request_id == request_id))
    )
    return RequestDetail(
        id=request.id,
        buyer_id=request.buyer_id,
        buyer_name=buyer.name,
        buyer_business_name=buyer.business_name,
        buyer_phone=buyer.whatsapp_number,
        raw_message=request.raw_message,
        status=request.status,
        comparison_payload=request.comparison_payload,
        buyer_decision=request.buyer_decision,
        created_at=request.created_at,
        updated_at=request.updated_at,
        messages=[MessageView.model_validate(message) for message in messages],
        requirement_versions=[RequirementVersionView.model_validate(version) for version in versions],
        approvals=[ApprovalView.model_validate(approval) for approval in approvals],
        rfq_ids=rfq_ids,
    )