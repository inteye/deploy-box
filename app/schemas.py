from datetime import datetime

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str
    slug: str
    adapter_type: str = "webhook_manifest_v1"
    workspace_path: str | None = None
    image_registry_prefix: str | None = None
    default_artifact_repository_id: int | None = None
    description: str | None = None


class ProjectRead(BaseModel):
    id: int
    name: str
    slug: str
    adapter_type: str
    workspace_path: str | None
    image_registry_prefix: str | None
    default_artifact_repository_id: int | None
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectUpdate(BaseModel):
    name: str
    slug: str
    workspace_path: str | None = None
    image_registry_prefix: str | None = None
    default_artifact_repository_id: int | None = None
    description: str | None = None


class EnvironmentCreate(BaseModel):
    name: str
    base_url: str | None = None
    webhook_url: str
    status_url: str
    shared_secret: str = Field(min_length=1)
    default_environment_name: str = "prod"


class EnvironmentUpdate(BaseModel):
    name: str
    base_url: str | None = None
    webhook_url: str
    status_url: str
    shared_secret: str = Field(min_length=1)
    default_environment_name: str = "prod"


class EnvironmentRead(BaseModel):
    id: int
    project_id: int
    name: str
    base_url: str | None
    webhook_url: str
    status_url: str
    default_environment_name: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ReleaseCreate(BaseModel):
    project_id: int
    version: str
    manifest_url: str
    commit: str | None = None
    created_by: str | None = None
    payload_json: str | None = None


class ReleaseRead(BaseModel):
    id: int
    project_id: int
    version: str
    manifest_url: str
    commit: str | None
    manifest_json: str | None
    manifest_sync_status: str
    manifest_sync_error: str | None
    component_count: int
    created_by: str | None
    payload_json: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DeploymentCreate(BaseModel):
    project_id: int
    environment_id: int
    release_id: int
    triggered_by: str | None = "manual"


class DeploymentRead(BaseModel):
    id: int
    project_id: int
    environment_id: int
    release_id: int
    status: str
    triggered_by: str | None
    request_payload_json: str | None
    adapter_response_json: str | None
    last_status_json: str | None
    log_excerpt: str | None
    submitted_at: datetime | None
    last_polled_at: datetime | None
    external_request_id: str | None
    status_reason: str | None
    progress_percent: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class BuildTemplateRead(BaseModel):
    id: int
    name: str
    slug: str
    strategy: str
    description: str | None
    config_json: str | None
    is_builtin: bool
    is_active: bool

    model_config = {"from_attributes": True}


class ProjectBuildConfigRead(BaseModel):
    id: int
    project_id: int
    template_id: int
    enabled: bool
    config_override_json: str | None

    model_config = {"from_attributes": True}


class BuildJobRead(BaseModel):
    id: int
    project_id: int
    template_id: int | None
    environment_id: int | None
    status: str
    artifact_mode: str
    source_type: str
    current_stage: str | None
    progress_percent: int
    output_version: str | None
    manifest_url: str | None
    storage_mode: str | None
    payload_json: str | None
    result_json: str | None
    error_message: str | None
    log_excerpt: str | None
    triggered_by: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class BuildJobEventRead(BaseModel):
    id: int
    build_job_id: int
    event_type: str
    stage: str | None
    message: str
    progress_percent: int | None
    created_at: datetime

    model_config = {"from_attributes": True}
