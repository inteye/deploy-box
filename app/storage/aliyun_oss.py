from urllib.parse import quote

import oss2


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
    def __init__(self, config: dict):
        self._config = config
        auth = oss2.Auth(config["access_key_id"], config["secret_access_key"])
        self._path_prefix = str(config.get("path_prefix") or "").strip().strip("/")
        self._bucket = oss2.Bucket(
            auth,
            _normalize_endpoint(str(config.get("endpoint") or "")),
            str(config["bucket_name"]).strip(),
            region=str(config.get("region") or "").strip() or None,
        )
        if config.get("custom_domain"):
            self._public_base_url = _normalize_base_url(str(config["custom_domain"]))
        else:
            self._public_base_url = _normalize_base_url(
                f"{config['bucket_name']}.{config['endpoint']}"
            )

    def _resolve_path(self, remote_path: str) -> str:
        relative = remote_path.lstrip("/")
        if self._path_prefix:
            return f"{self._path_prefix}/{relative}"
        return relative

    def upload_bytes(self, *, data: bytes, remote_path: str, content_type: str | None = None) -> str:
        resolved_path = self._resolve_path(remote_path)
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        result = self._bucket.put_object(resolved_path, data, headers=headers or None)
        if result.status not in {200, 201}:
            raise RuntimeError(f"oss upload failed: status={result.status}")
        return self.build_public_url(resolved_path)

    def build_public_url(self, remote_path: str) -> str:
        path = "/".join(quote(part, safe="-_.~/") for part in remote_path.split("/"))
        return f"{self._public_base_url}/{path.lstrip('/')}"
