import hashlib
import hmac
import json

import httpx

from ..config import get_settings
from ..models import Environment, Release
from .base import DeployAdapter


class WebhookManifestV1Adapter(DeployAdapter):
    def __init__(self, environment: Environment) -> None:
        super().__init__(environment)
        self.settings = get_settings()

    def trigger_deploy(self, release: Release, triggered_by: str | None) -> dict:
        manifest_json = None
        if release.manifest_json:
            try:
                parsed = json.loads(release.manifest_json)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                manifest_json = parsed
        payload = {
            "version": release.version,
            "manifest_url": release.manifest_url,
            "manifest_json": manifest_json,
            "environment": self.environment.default_environment_name,
            "triggered_by": triggered_by or "manual",
            "commit": release.commit or "",
        }
        raw_body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(
            self.environment.shared_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Release-Version": release.version,
            "X-Signature": f"sha256={signature}",
        }
        with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
            response = client.post(self.environment.webhook_url, content=raw_body, headers=headers)
        response.raise_for_status()
        return {
            "request_payload": payload,
            "response_status_code": response.status_code,
            "response_json": response.json(),
        }

    def fetch_status(self) -> dict:
        with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
            response = client.get(self.environment.status_url)
        response.raise_for_status()
        return response.json()
