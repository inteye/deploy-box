from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    app_name: str = "DeployBox"
    database_url: str = f"sqlite:///{(DATA_DIR / 'deploy-console.db').as_posix()}"
    request_timeout_seconds: int = 30
    deployment_trigger_timeout_seconds: int = 90
    deployment_poll_interval_seconds: int = 5
    deployment_watch_timeout_seconds: int = 1800
    secret_key: str = "replace-me-in-production"
    admin_username: str = "admin"
    admin_password: str = "change-me-now"
    use_oss: bool = Field(default=False, validation_alias="USE_OSS")
    oss_access_key_id: str = Field(default="", validation_alias="OSS_ACCESS_KEY_ID")
    oss_access_key_secret: str = Field(default="", validation_alias="OSS_ACCESS_KEY_SECRET")
    oss_bucket_name: str = Field(default="", validation_alias="OSS_BUCKET_NAME")
    oss_region: str = Field(default="", validation_alias="OSS_REGION")
    oss_endpoint: str = Field(default="", validation_alias="OSS_ENDPOINT")
    oss_custom_domain: str = Field(default="", validation_alias="OSS_CUSTOM_DOMAIN")
    workspace_path: str = "/workspace"
    package_script: str = "deploy/scripts/package_release.sh"
    package_artifact_base_url: str = "https://placeholder.invalid/releases"
    package_artifact_public_base_url: str = "http://host.docker.internal:18081/releases"
    local_artifacts_path: str = "/artifacts/releases"

    model_config = SettingsConfigDict(
        env_prefix="DEPLOY_CONSOLE_",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
