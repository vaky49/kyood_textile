from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.models import Approval, Buyer, Job, ProcurementRequest
from app.domain.statuses import ApprovalStatus, ApprovalType
from app.infrastructure.db.base import Base
from app.infrastructure.db.session import get_session
from app.main import app
from app.settings import Settings, get_settings


@pytest.mark.asyncio
async def test_approval_requires_auth_valid_candidates_and_is_idempotent() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        buyer = Buyer(whatsapp_number="+919876543210")
        session.add(buyer)
        await session.flush()
        procurement_request = ProcurementRequest(buyer_id=buyer.id, raw_message="Need suits")
        session.add(procurement_request)
        await session.flush()
        approval = Approval(
            request_id=procurement_request.id,
            approval_type=ApprovalType.SUPPLIER_SELECTION,
            thread_id=str(procurement_request.id),
            interrupt_key=f"{procurement_request.id}:select_suppliers",
            payload={
                "action": "select_suppliers",
                "candidates": [{"supplier_id": "SUP-1"}, {"supplier_id": "SUP-2"}],
            },
        )
        session.add(approval)
        await session.commit()
        approval_id = str(approval.id)
        approval_db_id = approval.id

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
            denied = await client.get("/api/approvals")
            assert denied.status_code == 401
            headers = {"X-Operator-Token": "test-token", "X-Operator-ID": "operator-7"}

            listed = await client.get("/api/approvals", headers=headers)
            assert listed.status_code == 200
            assert len(listed.json()) == 1

            invalid = await client.post(
                f"/api/approvals/{approval_id}/approve",
                headers=headers,
                json={"selected_supplier_ids": ["SUP-UNKNOWN"]},
            )
            assert invalid.status_code == 422

            approved = await client.post(
                f"/api/approvals/{approval_id}/approve",
                headers=headers,
                json={"selected_supplier_ids": ["SUP-1"]},
            )
            duplicate = await client.post(
                f"/api/approvals/{approval_id}/approve",
                headers=headers,
                json={"selected_supplier_ids": ["SUP-1"]},
            )

            assert approved.status_code == 202
            assert approved.json()["status"] == "approved"
            assert approved.json()["decided_by"] == "operator-7"
            assert approved.json()["decision_payload"] == {"selected_supplier_ids": ["SUP-1"]}
            assert duplicate.status_code == 409

            async with session_factory() as session:
                job_count = await session.scalar(select(func.count()).select_from(Job))
                updated_approval = await session.get(Approval, approval_db_id)
                assert job_count == 1
                assert updated_approval is not None
                assert updated_approval.status is ApprovalStatus.APPROVED
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        await engine.dispose()


@pytest.mark.asyncio
async def test_reject_decision_enqueues_resume_payload() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        buyer = Buyer(whatsapp_number="+911234567890")
        session.add(buyer)
        await session.flush()
        procurement_request = ProcurementRequest(buyer_id=buyer.id)
        session.add(procurement_request)
        await session.flush()
        approval = Approval(
            request_id=procurement_request.id,
            approval_type=ApprovalType.RFQ,
            thread_id=str(procurement_request.id),
            interrupt_key=f"{procurement_request.id}:approve_rfq",
            payload={"action": "approve_rfq"},
        )
        session.add(approval)
        await session.commit()
        approval_id = str(approval.id)

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
            rejected = await client.post(
                f"/api/approvals/{approval_id}/reject",
                headers={"X-Operator-Token": "test-token"},
                json={"note": "Please revise suppliers"},
            )

        assert rejected.status_code == 202
        assert rejected.json()["decision_payload"] == {"approved": False}
        async with session_factory() as session:
            job = await session.scalar(select(Job))
            assert job is not None
            assert job.job_type == "resume_graph"
            assert job.payload["approval_id"] == approval_id
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        await engine.dispose()