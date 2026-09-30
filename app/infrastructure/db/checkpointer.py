from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.settings import get_settings


def _postgres_driver_url(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@asynccontextmanager
async def postgres_checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    connection_string = _postgres_driver_url(get_settings().database_url)
    async with AsyncPostgresSaver.from_conn_string(connection_string) as checkpointer:
        await checkpointer.setup()
        yield checkpointer