import mimetypes
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response

from app.api.auth import require_operator
from app.infrastructure.storage.local import LocalMediaStorage
from app.settings import Settings, get_settings

router = APIRouter(prefix="/api/media", tags=["media"])
OperatorAccess = Annotated[None, Depends(require_operator)]
ApplicationSettings = Annotated[Settings, Depends(get_settings)]


@router.get("/{object_key}")
async def get_media(
    object_key: str,
    _operator: OperatorAccess,
    settings: ApplicationSettings,
) -> Response:
    try:
        content = await LocalMediaStorage(settings.media_storage_dir).read(object_key)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found") from error
    content_type = mimetypes.guess_type(Path(object_key).name)[0] or "application/octet-stream"
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )