from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.models import Job
from app.domain.statuses import JobStatus
from app.infrastructure.db.base import Base
from app.infrastructure.db.jobs import (
    claim_next_job,
    complete_job,
    enqueue_job,
    fail_job,
)


@pytest.mark.asyncio
async def test_job_enqueue_is_idempotent_and_claim_increments_attempt() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        first, created_first = await enqueue_job(
            session,
            job_type="process_request",
            payload={"request_id": "REQ-1"},
            idempotency_key="message:MSG-1",
        )
        second, created_second = await enqueue_job(
            session,
            job_type="process_request",
            payload={"request_id": "REQ-OTHER"},
            idempotency_key="message:MSG-1",
        )

        lease = await claim_next_job(session)

        assert created_first is True
        assert created_second is False
        assert first.id == second.id
        assert lease is not None
        assert lease.id == first.id
        assert lease.attempt_count == 1
        assert await claim_next_job(session) is None

        await complete_job(session, lease.id)
        completed = await session.get(Job, lease.id)
        assert completed is not None
        assert completed.status is JobStatus.SUCCEEDED

    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_lease_can_be_reclaimed_and_retries_are_bounded() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)

    async with session_factory() as session:
        job, _ = await enqueue_job(
            session,
            job_type="send_rfq",
            payload={"rfq_id": "RFQ-1"},
            idempotency_key="rfq-send:RFQ-1",
            run_after=now,
        )
        job.max_attempts = 3
        await session.commit()
        first_lease = await claim_next_job(session, lease_seconds=1, now=now)
        assert first_lease is not None

        second_lease = await claim_next_job(
            session,
            lease_seconds=1,
            now=now + timedelta(seconds=2),
        )
        assert second_lease is not None
        assert second_lease.id == first_lease.id
        assert second_lease.attempt_count == 2

        status = await fail_job(
            session,
            job.id,
            error_summary="transient failure",
            now=now,
            base_retry_seconds=10,
        )
        retryable = await session.get(Job, job.id)
        assert status is JobStatus.RETRYABLE_FAILED
        assert retryable is not None
        assert retryable.run_after == now + timedelta(seconds=20)

        retry_lease = await claim_next_job(session, now=now + timedelta(seconds=20))
        assert retry_lease is not None
        assert retry_lease.attempt_count == 3

        status = await fail_job(session, job.id, error_summary="permanent", now=now)
        dead = await session.get(Job, job.id)
        assert status is JobStatus.DEAD
        assert dead is not None
        assert dead.status is JobStatus.DEAD

    await engine.dispose()