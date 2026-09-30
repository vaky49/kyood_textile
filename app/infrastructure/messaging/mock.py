from dataclasses import dataclass


@dataclass(frozen=True)
class MockMessage:
    recipient: str
    text: str
    idempotency_key: str


class MockMessagingProvider:
    def __init__(self) -> None:
        self.sent_messages: list[MockMessage] = []
        self._sent_keys: set[str] = set()

    async def send_text(self, phone_number: str, text: str, idempotency_key: str) -> None:
        if idempotency_key in self._sent_keys:
            return
        self._sent_keys.add(idempotency_key)
        self.sent_messages.append(MockMessage(phone_number, text, idempotency_key))

    async def send_media(
        self,
        phone_number: str,
        media_id: str,
        media_type: str,
        idempotency_key: str,
        caption: str | None = None,
    ) -> None:
        text = caption or f"[{media_type}:{media_id}]"
        await self.send_text(phone_number, text, idempotency_key)