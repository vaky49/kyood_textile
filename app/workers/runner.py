import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.runner import PermanentJobError, ProcurementJobProcessor
from app.infrastructure.db.jobs import (
    claim_next_job,
    complete_job,
    dead_letter_job,
    fail_job,
)

logger = logging.getLogger(__name__)


class DurableWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        processor: ProcurementJobProcessor,
        *,
        lease_seconds: int = 120,
    ) -> None:
        self._session_factory = session_factory
        self._processor = processor
        self._lease_seconds = lease_seconds

    async def run_once(self) -> bool:
        async with self._session_factory() as session:
            lease = await claim_next_job(session, lease_seconds=self._lease_seconds)
        if lease is None:
            return False

        try:
            await self._processor.process(lease)
        except PermanentJobError as error:
            async with self._session_factory() as session:
                await dead_letter_job(session, lease.id, error_summary=str(error))
            logger.error("job_dead_lettered job_id=%s error=%s", lease.id, error)
        except Exception as error:
            async with self._session_factory() as session:
                await fail_job(
                    session,
                    lease.id,
                    error_summary=f"{type(error).__name__}: {error}",
                )
            logger.exception("job_failed job_id=%s", lease.id)
        else:
            async with self._session_factory() as session:
                await complete_job(session, lease.id)
            logger.info("job_completed job_id=%s type=%s", lease.id, lease.job_type)
        return True


async def run_forever(worker: DurableWorker, *, poll_interval_seconds: float = 0.5) -> None:
    while True:
        processed = await worker.run_once()
        if not processed:
            await asyncio.sleep(poll_interval_seconds)