from pathlib import Path
from uuid import uuid4


class LocalMediaStorage:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    async def save(self, filename: str, content_type: str, content: bytes) -> str:
        suffix = Path(filename).suffix.lower()[:12]
        object_key = f"{uuid4().hex}{suffix}"
        destination = (self._root / object_key).resolve()
        if destination.parent != self._root:
            raise ValueError("Invalid media filename")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return object_key

    async def read(self, object_key: str) -> bytes:
        destination = (self._root / object_key).resolve()
        if destination.parent != self._root:
            raise ValueError("Invalid media object key")
        return destination.read_bytes()