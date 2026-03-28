from .aliyun_oss import AliyunOssStorage
from .base import ArtifactStorage


def build_artifact_storage(settings) -> ArtifactStorage:
    if not settings.use_oss:
        raise RuntimeError("OSS 未启用，请先设置 USE_OSS=true")
    required = {
        "OSS_ACCESS_KEY_ID": settings.oss_access_key_id,
        "OSS_ACCESS_KEY_SECRET": settings.oss_access_key_secret,
        "OSS_BUCKET_NAME": settings.oss_bucket_name,
        "OSS_ENDPOINT": settings.oss_endpoint,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"OSS 配置不完整，缺少: {', '.join(missing)}")
    return AliyunOssStorage(settings)
