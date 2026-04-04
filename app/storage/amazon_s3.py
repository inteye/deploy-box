from urllib.parse import quote

import boto3


def _normalize_endpoint(endpoint: str | None) -> str | None:
    raw = str(endpoint or "").strip()
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/")
    return f"https://{raw.rstrip('/')}"


class AmazonS3Storage:
    def __init__(self, config: dict):
        endpoint = _normalize_endpoint(config.get("endpoint"))
        self._bucket_name = str(config["bucket_name"]).strip()
        self._region = str(config.get("region") or "").strip() or None
        self._path_prefix = str(config.get("path_prefix") or "").strip().strip("/")
        self._client = boto3.client(
            "s3",
            aws_access_key_id=str(config["access_key_id"]).strip(),
            aws_secret_access_key=str(config["secret_access_key"]).strip(),
            region_name=self._region,
            endpoint_url=endpoint,
        )
        custom_domain = str(config.get("custom_domain") or "").strip()
        if custom_domain:
            if custom_domain.startswith("http://") or custom_domain.startswith("https://"):
                self._public_base_url = custom_domain.rstrip("/")
            else:
                self._public_base_url = f"https://{custom_domain.rstrip('/')}"
        elif endpoint:
            self._public_base_url = f"{endpoint.rstrip('/')}/{self._bucket_name}"
        elif self._region:
            self._public_base_url = f"https://{self._bucket_name}.s3.{self._region}.amazonaws.com"
        else:
            self._public_base_url = f"https://{self._bucket_name}.s3.amazonaws.com"

    def _resolve_key(self, remote_path: str) -> str:
        relative = remote_path.lstrip("/")
        if self._path_prefix:
            return f"{self._path_prefix}/{relative}"
        return relative

    def upload_bytes(self, *, data: bytes, remote_path: str, content_type: str | None = None) -> str:
        key = self._resolve_key(remote_path)
        extra = {"ContentType": content_type} if content_type else {}
        self._client.put_object(Bucket=self._bucket_name, Key=key, Body=data, **extra)
        return self.build_public_url(key)

    def build_public_url(self, remote_path: str) -> str:
        path = "/".join(quote(part, safe="-_.~/") for part in remote_path.split("/"))
        return f"{self._public_base_url}/{path.lstrip('/')}"
