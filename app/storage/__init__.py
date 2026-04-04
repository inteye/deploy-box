from .amazon_s3 import AmazonS3Storage
from .aliyun_oss import AliyunOssStorage
from .base import ArtifactStorage
from ..security import decrypt_secret


def build_artifact_storage(settings, repository=None) -> ArtifactStorage:
    if repository is not None:
        provider = str(repository.provider).strip().lower()
        config = {
            "access_key_id": decrypt_secret(repository.access_key_id_encrypted),
            "secret_access_key": decrypt_secret(repository.secret_access_key_encrypted),
            "bucket_name": repository.bucket_name,
            "region": repository.region,
            "endpoint": repository.endpoint,
            "custom_domain": repository.custom_domain,
            "path_prefix": repository.path_prefix,
        }
        if provider == "aliyun_oss":
            return AliyunOssStorage(config)
        if provider == "amazon_s3":
            return AmazonS3Storage(config)
        raise RuntimeError(f"unsupported_artifact_provider: {provider}")
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
    return AliyunOssStorage(
        {
            "access_key_id": settings.oss_access_key_id,
            "secret_access_key": settings.oss_access_key_secret,
            "bucket_name": settings.oss_bucket_name,
            "region": settings.oss_region,
            "endpoint": settings.oss_endpoint,
            "custom_domain": settings.oss_custom_domain,
            "path_prefix": None,
        }
    )


def build_oss_storage_descriptor(settings, repository=None) -> dict:
    if repository is not None:
        return {
            "provider": repository.provider,
            "bucket": repository.bucket_name,
            "endpoint": repository.endpoint,
            "region": repository.region or None,
            "path_prefix": repository.path_prefix or None,
        }
    return {
        "provider": "aliyun_oss",
        "bucket": settings.oss_bucket_name,
        "endpoint": settings.oss_endpoint,
        "region": settings.oss_region or None,
    }
