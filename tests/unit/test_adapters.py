import httpx
import pytest

from app.infrastructure.messaging.mock import MockMessagingProvider
from app.infrastructure.messaging.whatsapp_cloud import (
    WhatsAppCloudApiClient,
    WhatsAppMessagingProvider,
)
from app.infrastructure.storage.local import LocalMediaStorage
from app.settings import Settings


@pytest.mark.asyncio
async def test_whatsapp_cloud_send_uses_bearer_auth_and_idempotency() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid-outbound"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cloud_api = WhatsAppCloudApiClient(
        Settings(
            whatsapp_access_token="test-access-token",
            whatsapp_phone_number_id="phone-id-1",
        ),
        http_client=client,
    )
    provider = WhatsAppMessagingProvider(cloud_api)

    await provider.send_text("+919876543210", "RFQ details", "rfq:1:supplier:1")
    await provider.send_text("+919876543210", "RFQ details", "rfq:1:supplier:1")

    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer test-access-token"
    assert requests[0].url.path.endswith("/v24.0/phone-id-1/messages")
    assert requests[0].read().decode().find("+919876543210") > 0
    await client.aclose()


@pytest.mark.asyncio
async def test_whatsapp_media_download_returns_bytes_and_mime_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.facebook.com":
            return httpx.Response(
                200,
                json={"url": "https://lookaside.fbsbx.com/media-1", "mime_type": "image/jpeg"},
            )
        return httpx.Response(200, content=b"image-bytes", headers={"content-type": "image/jpeg"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cloud_api = WhatsAppCloudApiClient(
        Settings(whatsapp_access_token="test-access-token", whatsapp_phone_number_id="phone-id-1"),
        http_client=client,
    )

    content, content_type = await cloud_api.get_media("media-1")

    assert content == b"image-bytes"
    assert content_type == "image/jpeg"
    await client.aclose()


@pytest.mark.asyncio
async def test_local_media_storage_round_trips_and_rejects_traversal(tmp_path) -> None:
    storage = LocalMediaStorage(tmp_path / "uploads")

    object_key = await storage.save("suit.jpg", "image/jpeg", b"image-content")

    assert await storage.read(object_key) == b"image-content"
    with pytest.raises(ValueError, match="Invalid media object key"):
        await storage.read("../secrets.txt")


@pytest.mark.asyncio
async def test_mock_messaging_deduplicates_retries() -> None:
    provider = MockMessagingProvider()

    await provider.send_text("+919876543210", "Hello", "message-1")
    await provider.send_text("+919876543210", "Hello", "message-1")

    assert len(provider.sent_messages) == 1