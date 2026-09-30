import base64
from pathlib import Path

from app.infrastructure.messaging.whatsapp_cloud import WhatsAppCloudApiClient
from app.infrastructure.storage.local import LocalMediaStorage
from app.settings import Settings


class WhatsAppMediaLoader:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._storage = LocalMediaStorage(settings.media_storage_dir)

    async def load_for_vision(
        self,
        media_id: str,
        existing_object_key: str | None = None,
    ) -> tuple[str, str]:
        if existing_object_key is not None:
            content = await self._storage.read(existing_object_key)
            content_type = _content_type_from_suffix(existing_object_key)
            object_key = existing_object_key
        else:
            async with _cloud_api(self._settings) as cloud_api:
                content, content_type = await cloud_api.get_media(media_id)
            suffix = _suffix_for_content_type(content_type)
            object_key = await self._storage.save(f"whatsapp-{media_id}{suffix}", content_type, content)
        encoded = base64.b64encode(content).decode("ascii")
        return object_key, f"data:{content_type};base64,{encoded}"


class _CloudApiContext:
    def __init__(self, settings: Settings) -> None:
        self._client = WhatsAppCloudApiClient(settings)

    async def __aenter__(self) -> WhatsAppCloudApiClient:
        return self._client

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self._client.aclose()


def _cloud_api(settings: Settings) -> _CloudApiContext:
    return _CloudApiContext(settings)


def _suffix_for_content_type(content_type: str) -> str:
    suffixes = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
        "audio/ogg": ".ogg",
    }
    return suffixes.get(content_type.split(";")[0].casefold(), ".bin")


def _content_type_from_suffix(object_key: str) -> str:
    content_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".pdf": "application/pdf",
        ".ogg": "audio/ogg",
    }
    return content_types.get(Path(object_key).suffix.casefold(), "application/octet-stream")