from typing import Protocol


class MessagingProvider(Protocol):
    async def send_text(self, phone_number: str, text: str, idempotency_key: str) -> None: ...

    async def send_media(
        self,
        phone_number: str,
        media_id: str,
        media_type: str,
        idempotency_key: str,
        caption: str | None = None,
    ) -> None: ...


class MediaStorage(Protocol):
    async def save(self, filename: str, content_type: str, content: bytes) -> str: ...

    async def read(self, object_key: str) -> bytes: ...