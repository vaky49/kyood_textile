from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Job
from app.domain.statuses import JobStatus


@dataclass(frozen=True)
class JobLease:
    id: UUID
    job_type: str
    payload: dict[str, Any]
    attempt_count: int


async def enqueue_job(
    session: AsyncSession,
    *,
    job_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    run_after: datetime | None = None,
) -> tuple[Job, bool]:
    dialect = session.get_bind().dialect.name
    insert = postgres_insert if dialect == "postgresql" else sqlite_insert
    statement = (
        insert(Job)
        .values(
            job_type=job_type,
            payload=payload,
            idempotency_key=idempotency_key,
            run_after=run_after or datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=[Job.idempotency_key])
        .returning(Job.id)
    )
    inserted_id = (await session.execute(statement)).scalar_one_or_none()
    created = inserted_id is not None
    job_id = inserted_id or await session.scalar(
        select(Job.id).where(Job.idempotency_key == idempotency_key)
    )
    if job_id is None:
        raise RuntimeError("Job insert conflicted but the existing job could not be found")
    job = await session.get(Job, job_id)
    if job is None:
        raise RuntimeError("Job record disappeared after insertion")
    return job, created


async def claim_next_job(
    session: AsyncSession,
    *,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> JobLease | None:
    current_time = now or datetime.now(UTC)
    eligible = or_(
        (
            Job.status.in_([JobStatus.QUEUED, JobStatus.RETRYABLE_FAILED])
            & (Job.run_after <= current_time)
        ),
        (Job.status == JobStatus.RUNNING) & (Job.lease_until <= current_time),
    )
    statement = (
        select(Job)
        .where(eligible)
        .order_by(Job.run_after, Job.created_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = await session.scalar(statement)
    if job is None:
        return None

    job.status = JobStatus.RUNNING
    job.lease_until = current_time + timedelta(seconds=lease_seconds)
    job.attempt_count += 1
    await session.commit()
    return JobLease(
        id=job.id,
        job_type=job.job_type,
        payload=dict(job.payload),
        attempt_count=job.attempt_count,
    )


async def complete_job(session: AsyncSession, job_id: UUID) -> None:
    job = await session.get(Job, job_id)
    if job is None:
        raise LookupError("Job not found")
    job.status = JobStatus.SUCCEEDED
    job.lease_until = None
    job.error_summary = None
    await session.commit()


async def dead_letter_job(session: AsyncSession, job_id: UUID, *, error_summary: str) -> None:
    job = await session.get(Job, job_id)
    if job is None:
        raise LookupError("Job not found")
    job.status = JobStatus.DEAD
    job.lease_until = None
    job.error_summary = error_summary[:2000]
    await session.commit()


async def fail_job(
    session: AsyncSession,
    job_id: UUID,
    *,
    error_summary: str,
    now: datetime | None = None,
    base_retry_seconds: int = 5,
    max_retry_seconds: int = 300,
) -> JobStatus:
    job = await session.get(Job, job_id)
    if job is None:
        raise LookupError("Job not found")

    current_time = now or datetime.now(UTC)
    job.lease_until = None
    job.error_summary = error_summary[:2000]
    if job.attempt_count >= job.max_attempts:
        job.status = JobStatus.DEAD
    else:
        job.status = JobStatus.RETRYABLE_FAILED
        retry_seconds = min(
            base_retry_seconds * (2 ** max(job.attempt_count - 1, 0)),
            max_retry_seconds,
        )
        job.run_after = current_time + timedelta(seconds=retry_seconds)
    await session.commit()
    return job.status