from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "deploy_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    adapter_type: Mapped[str] = mapped_column(String(80), nullable=False, default="webhook_manifest_v1")
    workspace_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_registry_prefix: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_artifact_repository_id: Mapped[int | None] = mapped_column(
        ForeignKey("deploy_artifact_repositories.id"), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    environments: Mapped[list["Environment"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    releases: Mapped[list["Release"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    deployments: Mapped[list["Deployment"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    build_config: Mapped["ProjectBuildConfig | None"] = relationship(
        back_populates="project", cascade="all, delete-orphan", uselist=False
    )
    build_jobs: Mapped[list["BuildJob"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    components: Mapped[list["ProjectComponent"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    default_artifact_repository: Mapped["ArtifactRepository | None"] = relationship(
        foreign_keys=[default_artifact_repository_id]
    )
    role_assignments: Mapped[list["OperatorUserRole"]] = relationship(back_populates="project")
    governance: Mapped["ProjectGovernance | None"] = relationship(
        back_populates="project", cascade="all, delete-orphan", uselist=False
    )
    quality_checks: Mapped[list["QualityCheckRun"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectComponent(Base):
    __tablename__ = "deploy_project_components"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_deploy_project_component_project_name"),
        UniqueConstraint("project_id", "service_name", name="uq_deploy_project_component_project_service"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("deploy_projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    service_name: Mapped[str] = mapped_column(String(120), nullable=False)
    image: Mapped[str] = mapped_column(String(255), nullable=False)
    dockerfile: Mapped[str] = mapped_column(String(255), nullable=False, default="./Dockerfile")
    context_path: Mapped[str] = mapped_column(String(255), nullable=False, default=".")
    tar_name_pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    build_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    project: Mapped["Project"] = relationship(back_populates="components")


class Environment(Base):
    __tablename__ = "deploy_environments"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_deploy_environment_project_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("deploy_projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    webhook_url: Mapped[str] = mapped_column(String(500), nullable=False)
    status_url: Mapped[str] = mapped_column(String(500), nullable=False)
    shared_secret: Mapped[str] = mapped_column(String(255), nullable=False)
    default_environment_name: Mapped[str] = mapped_column(String(80), nullable=False, default="prod")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    project: Mapped["Project"] = relationship(back_populates="environments")
    deployments: Mapped[list["Deployment"]] = relationship(back_populates="environment")


class Release(Base):
    __tablename__ = "deploy_releases"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_deploy_release_project_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("deploy_projects.id"), nullable=False)
    version: Mapped[str] = mapped_column(String(120), nullable=False)
    manifest_url: Mapped[str] = mapped_column(String(500), nullable=False)
    commit: Mapped[str | None] = mapped_column(String(80), nullable=True)
    manifest_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_sync_status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    manifest_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    component_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    storage_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    project: Mapped["Project"] = relationship(back_populates="releases")
    deployments: Mapped[list["Deployment"]] = relationship(back_populates="release")
    components: Mapped[list["ReleaseComponent"]] = relationship(
        back_populates="release", cascade="all, delete-orphan"
    )


class ReleaseComponent(Base):
    __tablename__ = "deploy_release_components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("deploy_releases.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    image: Mapped[str] = mapped_column(String(255), nullable=False)
    image_tar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    image_tar_sha256: Mapped[str | None] = mapped_column(String(255), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    release: Mapped["Release"] = relationship(back_populates="components")


class Deployment(Base):
    __tablename__ = "deploy_deployments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("deploy_projects.id"), nullable=False)
    environment_id: Mapped[int] = mapped_column(ForeignKey("deploy_environments.id"), nullable=False)
    release_id: Mapped[int] = mapped_column(ForeignKey("deploy_releases.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    triggered_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    request_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    adapter_response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_status_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped["Project"] = relationship(back_populates="deployments")
    environment: Mapped["Environment"] = relationship(back_populates="deployments")
    release: Mapped["Release"] = relationship(back_populates="deployments")


class BuildTemplate(Base):
    __tablename__ = "deploy_build_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    strategy: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    project_configs: Mapped[list["ProjectBuildConfig"]] = relationship(back_populates="template")


class ProjectBuildConfig(Base):
    __tablename__ = "deploy_project_build_configs"
    __table_args__ = (UniqueConstraint("project_id", name="uq_deploy_project_build_config_project"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("deploy_projects.id"), nullable=False)
    template_id: Mapped[int] = mapped_column(ForeignKey("deploy_build_templates.id"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config_override_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    project: Mapped["Project"] = relationship(back_populates="build_config")
    template: Mapped["BuildTemplate"] = relationship(back_populates="project_configs")


class BuildJob(Base):
    __tablename__ = "deploy_build_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("deploy_projects.id"), nullable=False)
    template_id: Mapped[int | None] = mapped_column(ForeignKey("deploy_build_templates.id"), nullable=True)
    environment_id: Mapped[int | None] = mapped_column(ForeignKey("deploy_environments.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    artifact_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="auto")
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="generated")
    current_stage: Mapped[str | None] = mapped_column(String(120), nullable=True)
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    manifest_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    storage_mode: Mapped[str | None] = mapped_column(String(40), nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped["Project"] = relationship(back_populates="build_jobs")
    template: Mapped["BuildTemplate | None"] = relationship()
    environment: Mapped["Environment | None"] = relationship()
    events: Mapped[list["BuildJobEvent"]] = relationship(back_populates="build_job", cascade="all, delete-orphan")


class BuildJobEvent(Base):
    __tablename__ = "deploy_build_job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    build_job_id: Mapped[int] = mapped_column(ForeignKey("deploy_build_jobs.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, default="info")
    stage: Mapped[str | None] = mapped_column(String(120), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    progress_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    build_job: Mapped["BuildJob"] = relationship(back_populates="events")


class OperatorUser(Base):
    __tablename__ = "deploy_operator_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    preferred_lang: Mapped[str | None] = mapped_column(String(10), nullable=True, default=None)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    role_assignments: Mapped[list["OperatorUserRole"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="actor")


class OperatorUserRole(Base):
    __tablename__ = "deploy_operator_user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_code", "project_id", name="uq_deploy_operator_user_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("deploy_operator_users.id"), nullable=False)
    role_code: Mapped[str] = mapped_column(String(80), nullable=False)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("deploy_projects.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    user: Mapped["OperatorUser"] = relationship(back_populates="role_assignments")
    project: Mapped["Project | None"] = relationship(back_populates="role_assignments")


class ArtifactRepository(Base):
    __tablename__ = "deploy_artifact_repositories"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_deploy_artifact_repository_slug"),
        UniqueConstraint("name", name="uq_deploy_artifact_repository_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    bucket_name: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    custom_domain: Mapped[str | None] = mapped_column(String(500), nullable=True)
    path_prefix: Mapped[str | None] = mapped_column(String(255), nullable=True)
    access_key_id_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    secret_access_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class SystemSetting(Base):
    __tablename__ = "deploy_system_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class AuditLog(Base):
    __tablename__ = "deploy_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("deploy_operator_users.id"), nullable=True)
    actor_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    request_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    actor: Mapped["OperatorUser | None"] = relationship(back_populates="audit_logs")


class ProjectGovernance(Base):
    __tablename__ = "deploy_project_governance"
    __table_args__ = (UniqueConstraint("project_id", name="uq_deploy_project_governance_project"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("deploy_projects.id"), nullable=False)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("deploy_operator_users.id"), nullable=True)
    release_owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("deploy_operator_users.id"), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(40), nullable=False, default="medium")
    release_checklist_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    project: Mapped["Project"] = relationship(back_populates="governance")
    owner_user: Mapped["OperatorUser | None"] = relationship(foreign_keys=[owner_user_id])
    release_owner_user: Mapped["OperatorUser | None"] = relationship(foreign_keys=[release_owner_user_id])


class QualityCheckRun(Base):
    __tablename__ = "deploy_quality_check_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("deploy_projects.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    project: Mapped["Project"] = relationship(back_populates="quality_checks")
