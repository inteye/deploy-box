from ..models import Environment
from .base import DeployAdapter
from .webhook_manifest_v1 import WebhookManifestV1Adapter


def build_adapter(adapter_type: str, environment: Environment) -> DeployAdapter:
    if adapter_type == "webhook_manifest_v1":
        return WebhookManifestV1Adapter(environment)
    raise ValueError(f"unsupported adapter type: {adapter_type}")
