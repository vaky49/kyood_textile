import asyncio
import logging

from app.agent.runner import ProcurementJobProcessor
from app.infrastructure.db.checkpointer import postgres_checkpointer
from app.infrastructure.db.session import SessionFactory
from app.infrastructure.llm.requirements import build_requirement_extractor
from app.infrastructure.messaging.media import WhatsAppMediaLoader
from app.infrastructure.messaging.workflow import build_messaging_provider
from app.settings import get_settings
from app.workers.runner import DurableWorker, run_forever


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    messaging = build_messaging_provider(settings, SessionFactory)
    media_loader = (
        WhatsAppMediaLoader(settings)
        if settings.whatsapp_access_token and settings.whatsapp_phone_number_id
        else None
    )
    processor = ProcurementJobProcessor(
        SessionFactory,
        checkpointer_factory=postgres_checkpointer,
        extractor=build_requirement_extractor(settings),
        messaging=messaging,
        buyer_messaging=messaging,
        media_loader=media_loader,
    )
    await run_forever(DurableWorker(SessionFactory, processor))


if __name__ == "__main__":
    asyncio.run(main())