from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_operator
from app.domain.models import Approval
from app.domain.statuses import ApprovalStatus, ApprovalType
from app.infrastructure.db.jobs import enqueue_job
from app.infrastructure.db.session import get_session

router = APIRouter(prefix="/api/approvals", tags=["approvals"])
OperatorAccess = Annotated[None, Depends(require_operator)]
DatabaseSession = Annotated[AsyncSession, Depends(get_session)]


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_supplier_ids: list[str] | None = None
    note: str | None = Field(default=None, max_length=2000)


class ApprovalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    request_id: UUID
    approval_type: ApprovalType
    thread_id: str
    status: ApprovalStatus
    payload: dict[str, Any]
    decision_payload: dict[str, Any] | None
    decided_by: str | None
    decision_note: str | None
    created_at: datetime
    decided_at: datetime | None


@router.get("", response_model=list[ApprovalRead])
async def list_approvals(
    session: DatabaseSession,
    _operator: OperatorAccess,
    approval_status: Annotated[ApprovalStatus, Query(alias="status")] = ApprovalStatus.PENDING,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[Approval]:
    result = await session.scalars(
        select(Approval)
        .where(Approval.status == approval_status)
        .order_by(Approval.created_at, Approval.id)
        .limit(limit)
    )
    return list(result)


@router.post("/{approval_id}/approve", response_model=ApprovalRead, status_code=202)
async def approve(
    approval_id: UUID,
    decision: ApprovalDecision,
    session: DatabaseSession,
    _operator: OperatorAccess,
    operator_id: Annotated[str | None, Header(alias="X-Operator-ID")] = None,
) -> Approval:
    approval = await _pending_approval(session, approval_id)
    decision_payload = _decision_payload(approval, decision, approved=True)
    return await _save_decision(
        session,
        approval,
        decision_payload,
        status=ApprovalStatus.APPROVED,
        operator_id=operator_id,
        decision_note=decision.note,
    )


@router.post("/{approval_id}/reject", response_model=ApprovalRead, status_code=202)
async def reject(
    approval_id: UUID,
    decision: ApprovalDecision,
    session: DatabaseSession,
    _operator: OperatorAccess,
    operator_id: Annotated[str | None, Header(alias="X-Operator-ID")] = None,
) -> Approval:
    approval = await _pending_approval(session, approval_id)
    decision_payload = _decision_payload(approval, decision, approved=False)
    return await _save_decision(
        session,
        approval,
        decision_payload,
        status=ApprovalStatus.REJECTED,
        operator_id=operator_id,
        decision_note=decision.note,
    )


async def _pending_approval(session: AsyncSession, approval_id: UUID) -> Approval:
    approval = await session.scalar(
        select(Approval).where(Approval.id == approval_id).with_for_update()
    )
    if approval is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")
    if approval.status is not ApprovalStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Approval is already decided")
    return approval


def _decision_payload(
    approval: Approval,
    decision: ApprovalDecision,
    *,
    approved: bool,
) -> dict[str, Any]:
    if approval.approval_type is ApprovalType.SUPPLIER_SELECTION:
        if approved:
            selected_ids = decision.selected_supplier_ids or []
            candidates = {
                candidate["supplier_id"] for candidate in approval.payload.get("candidates", [])
            }
            if not selected_ids or len(selected_ids) != len(set(selected_ids)):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Select one or more unique supplier candidates",
                )
            if not set(selected_ids) <= candidates:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Selection contains an unknown supplier",
                )
            return {"selected_supplier_ids": selected_ids}
        return {"selected_supplier_ids": []}

    if decision.selected_supplier_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Supplier selection is only valid for supplier-selection approvals",
        )
    return {"approved": approved}


async def _save_decision(
    session: AsyncSession,
    approval: Approval,
    decision_payload: dict[str, Any],
    *,
    status: ApprovalStatus,
    operator_id: str | None,
    decision_note: str | None,
) -> Approval:
    approval.status = status
    approval.decision_payload = decision_payload
    approval.decided_by = operator_id or "operator"
    approval.decision_note = decision_note
    approval.decided_at = datetime.now(UTC)
    await enqueue_job(
        session,
        job_type="resume_graph",
        payload={"request_id": str(approval.request_id), "approval_id": str(approval.id)},
        idempotency_key=f"resume-approval:{approval.id}",
    )
    await session.commit()
    await session.refresh(approval)
    return approval