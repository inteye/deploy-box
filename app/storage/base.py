from typing import Protocol


class ArtifactStorage(Protocol):
    def upload_bytes(self, *, data: bytes, remote_path: str, content_type: str | None = None) -> str:
        ...
