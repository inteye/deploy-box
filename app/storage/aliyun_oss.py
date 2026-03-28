from urllib.parse import quote

import oss2

from ..config import Settings


def _normalize_endpoint(endpoint: str) -> str:
    value = endpoint.strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value.rstrip("/")
    return f"https://{value.rstrip('/')}"


def _normalize_base_url(base_url: str) -> str:
    value = base_url.strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value.rstrip("/")
    return f"https://{value.rstrip('/')}"


class AliyunOssStorage:
    def __init__(self, settings: Settings):
        self._settings = settings
        auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
        self._bucket = oss2.Bucket(
            auth,
            _normalize_endpoint(settings.oss_endpoint),
            settings.oss_bucket_name,
            region=settings.oss_region or None,
        )
        if settings.oss_custom_domain:
            self._public_base_url = _normalize_base_url(settings.oss_custom_domain)
        else:
            self._public_base_url = _normalize_base_url(
                f"{settings.oss_bucket_name}.{settings.oss_endpoint}"
            )

    def upload_bytes(self, *, data: bytes, remote_path: str, content_type: str | None = None) -> str:
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        result = self._bucket.put_object(remote_path, data, headers=headers or None)
        if result.status not in {200, 201}:
            raise RuntimeError(f"oss upload failed: status={result.status}")
        return self.build_public_url(remote_path)

    def build_public_url(self, remote_path: str) -> str:
        path = "/".join(quote(part, safe="-_.~/") for part in remote_path.split("/"))
        return f"{self._public_base_url}/{path.lstrip('/')}"
