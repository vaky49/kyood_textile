from typing import Any

import httpx

from app.infrastructure.messaging.protocols import MessagingProvider
from app.settings import Settings


class MessagingSendError(RuntimeError):
    pass


class WhatsAppCloudApiClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
            raise ValueError("WhatsApp access token and phone number ID are required")
        self._settings = settings
        self._client = http_client or httpx.AsyncClient(timeout=20)
        self._owns_client = http_client is None
        self._base_url = f"https://graph.facebook.com/{settings.whatsapp_api_version}"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_text(self, phone_number: str, text: str) -> str:
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "to": phone_number,
                "type": "text",
                "text": {"body": text},
            }
        )

    async def send_media(
        self,
        phone_number: str,
        media_id: str,
        media_type: str,
        caption: str | None = None,
    ) -> str:
        if media_type not in {"image", "document", "audio", "video"}:
            raise ValueError("Unsupported WhatsApp media type")
        media: dict[str, str] = {"id": media_id}
        if caption and media_type in {"image", "document", "video"}:
            media["caption"] = caption
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "to": phone_number,
                "type": media_type,
                media_type: media,
            }
        )

    async def get_media(self, media_id: str) -> tuple[bytes, str]:
        metadata = await self._request("GET", f"{self._base_url}/{media_id}")
        media_url = metadata.get("url")
        mime_type = metadata.get("mime_type", "application/octet-stream")
        if not isinstance(media_url, str):
            raise MessagingSendError("WhatsApp media metadata did not include a download URL")
        response = await self._request("GET", media_url, raw=True)
        return response.content, mime_type

    async def _send(self, payload: dict[str, Any]) -> str:
        result = await self._request(
            "POST",
            f"{self._base_url}/{self._settings.whatsapp_phone_number_id}/messages",
            json=payload,
        )
        messages = result.get("messages", [])
        if not messages or not isinstance(messages[0].get("id"), str):
            raise MessagingSendError("WhatsApp API did not return a message ID")
        return messages[0]["id"]

    async def _request(self, method: str, url: str, *, raw: bool = False, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self._settings.whatsapp_access_token}"}
        try:
            response = await self._client.request(method, url, headers=headers, **kwargs)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise MessagingSendError("WhatsApp Cloud API request failed") from error
        if raw:
            return response
        try:
            return response.json()
        except ValueError as error:
            raise MessagingSendError("WhatsApp Cloud API returned invalid JSON") from error


class WhatsAppMessagingProvider(MessagingProvider):
    def __init__(self, client: WhatsAppCloudApiClient) -> None:
        self._client = client
        self._sent_keys: set[str] = set()

    async def send_text(self, phone_number: str, text: str, idempotency_key: str) -> None:
        if idempotency_key in self._sent_keys:
            return
        await self._client.send_text(phone_number, text)
        self._sent_keys.add(idempotency_key)

    async def send_media(
        self,
        phone_number: str,
        media_id: str,
        media_type: str,
        idempotency_key: str,
        caption: str | None = None,
    ) -> None:
        if idempotency_key in self._sent_keys:
            return
        await self._client.send_media(phone_number, media_id, media_type, caption)
        self._sent_keys.add(idempotency_key)