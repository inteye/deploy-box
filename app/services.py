import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from textwrap import dedent

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .adapters import build_adapter
from .config import get_settings
from .database import SessionLocal
from .models import (
    BuildJob,
    BuildJobEvent,
    BuildTemplate,
    Deployment,
    Environment,
    Project,
    ProjectBuildConfig,
    ProjectComponent,
    Release,
    ReleaseComponent,
)
from .storage import build_artifact_storage, build_oss_storage_descriptor
from .task_runner import submit_background_job


TERMINAL_DEPLOYMENT_STATUSES = {"succeeded", "failed", "timed_out_but_running"}
SUCCESS_AGENT_STATUSES = {"deployed", "success", "succeeded"}
FAILED_AGENT_STATUSES = {"failed"}


def ensure_builtin_templates(db: Session) -> None:
    templates = [
        {
            "name": "脚本打包模板",
            "slug": "manifest_script_v1",
            "strategy": "manifest_script_v1",
            "description": "执行项目已有打包脚本，产出 manifest 与多组件制品。",
            "config_json": json.dumps(
                {
                    "package_script": "deploy/scripts/package_release.sh",
                    "artifact_public_base_url": "http://host.docker.internal:18081/releases",
                },
                ensure_ascii=True,
            ),
        },
        {
            "name": "上传已有制品模板",
            "slug": "manifest_upload_v1",
            "strategy": "manifest_upload_v1",
            "description": "适合已有外部构建系统，仅在控制台登记与上传已有 release。",
            "config_json": json.dumps({}, ensure_ascii=True),
        },
    ]
    changed = False
    for item in templates:
        template = db.scalar(select(BuildTemplate).where(BuildTemplate.slug == item["slug"]))
        if template:
            expected_config = item["config_json"]
            if (not template.is_builtin) or template.config_json != expected_config or template.description != item["description"]:
                template.is_builtin = True
                template.description = item["description"]
                template.config_json = expected_config
                changed = True
            continue
        db.add(
            BuildTemplate(
                name=item["name"],
                slug=item["slug"],
                strategy=item["strategy"],
                description=item["description"],
                config_json=item["config_json"],
                is_builtin=True,
            )
        )
        changed = True
    if changed:
        db.commit()


def ensure_project_build_config(db: Session, project: Project) -> ProjectBuildConfig:
    build_config = db.scalar(select(ProjectBuildConfig).where(ProjectBuildConfig.project_id == project.id))
    if build_config:
        return build_config
    template = db.scalar(select(BuildTemplate).where(BuildTemplate.slug == "manifest_script_v1"))
    if not template:
        raise RuntimeError("未找到默认构建模板 manifest_script_v1")
    build_config = ProjectBuildConfig(project_id=project.id, template_id=template.id)
    db.add(build_config)
    db.commit()
    db.refresh(build_config)
    return build_config


def ensure_default_project_components(db: Session, project: Project) -> list[ProjectComponent]:
    existing = db.scalars(
        select(ProjectComponent).where(ProjectComponent.project_id == project.id).order_by(ProjectComponent.id.asc())
    ).all()
    if existing:
        return existing
    component = ProjectComponent(
        project_id=project.id,
        name="api",
        service_name="api",
        image=_default_component_image(project, "api"),
        dockerfile="./Dockerfile",
        context_path=".",
        tar_name_pattern=f"{project.slug}-api-__VERSION__.tar",
        build_enabled=True,
        enabled=True,
        default_selected=True,
        source_type="default",
    )
    db.add(component)
    db.commit()
    return db.scalars(
        select(ProjectComponent).where(ProjectComponent.project_id == project.id).order_by(ProjectComponent.id.asc())
    ).all()


def resolve_project_workspace(project: Project, settings=None) -> Path:
    settings = settings or get_settings()
    configured = (project.workspace_path or "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_absolute():
            return candidate.resolve()
        return (Path(settings.workspace_path).resolve() / candidate).resolve()
    return Path(settings.workspace_path).resolve()


def list_project_components(db: Session, project: Project) -> list[ProjectComponent]:
    components = db.scalars(
        select(ProjectComponent).where(ProjectComponent.project_id == project.id).order_by(ProjectComponent.id.asc())
    ).all()
    if components:
        return components
    return ensure_default_project_components(db, project)


def save_project_component(
    db: Session,
    *,
    project: Project,
    component_id: int | None,
    name: str,
    service_name: str,
    image: str,
    dockerfile: str,
    context_path: str,
    tar_name_pattern: str,
    build_enabled: bool,
    enabled: bool,
    default_selected: bool,
    source_type: str = "manual",
) -> ProjectComponent:
    normalized_name = name.strip()
    normalized_service = service_name.strip()
    if not normalized_name:
        raise RuntimeError("组件名不能为空")
    if not normalized_service:
        raise RuntimeError("Compose service 名不能为空")
    if not image.strip():
        raise RuntimeError("镜像地址不能为空")
    component = db.get(ProjectComponent, component_id) if component_id else None
    if component and component.project_id != project.id:
        raise RuntimeError("组件不存在")
    duplicate = db.scalar(
        select(ProjectComponent).where(
            ProjectComponent.project_id == project.id,
            ProjectComponent.name == normalized_name,
            ProjectComponent.id != (component.id if component else 0),
        )
    )
    if duplicate:
        raise RuntimeError(f"组件名已存在: {normalized_name}")
    duplicate_service = db.scalar(
        select(ProjectComponent).where(
            ProjectComponent.project_id == project.id,
            ProjectComponent.service_name == normalized_service,
            ProjectComponent.id != (component.id if component else 0),
        )
    )
    if duplicate_service:
        raise RuntimeError(f"Compose service 已存在: {normalized_service}")
    if not component:
        component = ProjectComponent(project_id=project.id)
    component.name = normalized_name
    component.service_name = normalized_service
    component.image = image.strip()
    component.dockerfile = dockerfile.strip() or "./Dockerfile"
    component.context_path = context_path.strip() or "."
    component.tar_name_pattern = tar_name_pattern.strip() or f"{project.slug}-{normalized_service}-__VERSION__.tar"
    component.build_enabled = build_enabled
    component.enabled = enabled
    component.default_selected = default_selected if enabled else False
    component.source_type = source_type
    db.add(component)
    db.commit()
    db.refresh(component)
    return component


def delete_project_component(db: Session, *, project: Project, component_id: int) -> None:
    component = db.get(ProjectComponent, component_id)
    if not component or component.project_id != project.id:
        raise RuntimeError("组件不存在")
    db.delete(component)
    db.commit()


def import_project_components_from_compose(
    db: Session,
    *,
    project: Project,
    compose_relative_path: str,
) -> tuple[Path, list[ProjectComponent]]:
    settings = get_settings()
    workspace = resolve_project_workspace(project, settings)
    compose_path = (workspace / compose_relative_path.strip()).resolve()
    if not compose_path.exists():
        raise RuntimeError(f"Compose 文件不存在: {compose_path}")
    services = _parse_compose_services(compose_path.read_text(encoding="utf-8"))
    if not services:
        raise RuntimeError("未在 compose 文件中解析到 services")
    imported: list[ProjectComponent] = []
    existing_components = db.scalars(select(ProjectComponent).where(ProjectComponent.project_id == project.id)).all()
    parsed_service_names = {str(item["service_name"]).strip() for item in services}

    # New projects get a default placeholder `api` component before the user imports
    # real compose services. Drop that bootstrap record if the compose file does not
    # actually define an `api` service, otherwise users see a phantom extra component.
    for component in existing_components:
        if (
            component.source_type == "default"
            and component.service_name == "api"
            and component.name == "api"
            and component.service_name not in parsed_service_names
        ):
            db.delete(component)
        elif component.source_type == "compose_import" and component.service_name not in parsed_service_names:
            db.delete(component)
    db.commit()

    existing_components = db.scalars(select(ProjectComponent).where(ProjectComponent.project_id == project.id)).all()
    existing_by_service = {item.service_name: item for item in existing_components}
    for item in services:
        service_name = item["service_name"]
        image = _unwrap_compose_image_reference(item["image"]) or _default_component_image(project, service_name)
        dockerfile = _normalize_compose_dockerfile_path(item["dockerfile"], item["context_path"])
        context_path = item["context_path"] or "."
        build_enabled = bool(item.get("has_build"))
        component = save_project_component(
            db,
            project=project,
            component_id=existing_by_service.get(service_name).id if service_name in existing_by_service else None,
            name=service_name,
            service_name=service_name,
            image=image,
            dockerfile=dockerfile,
            context_path=context_path,
            tar_name_pattern=f"{project.slug}-{service_name}-__VERSION__.tar",
            build_enabled=build_enabled,
            enabled=True,
            default_selected=build_enabled and not _is_infra_component(service_name, image),
            source_type="compose_import",
        )
        imported.append(component)
    return compose_path, imported


def build_starter_bundle(
    *,
    project: Project,
    build_config: ProjectBuildConfig | None,
    components: list[ProjectComponent],
    environment: Environment | None,
    lang: str = "zh",
) -> dict:
    settings = get_settings()
    resolved = _resolve_build_config(settings, build_config) if build_config and build_config.template else {
        "workspace_path": str(resolve_project_workspace(project, settings)),
        "package_script": settings.package_script,
        "artifact_public_base_url": settings.package_artifact_public_base_url or settings.package_artifact_base_url,
        "extra_env": {},
    }
    artifact_public_base_url = str(
        resolved.get("artifact_public_base_url")
        or settings.package_artifact_public_base_url
        or settings.package_artifact_base_url
    ).rstrip("/")
    project_slug = project.slug.strip()
    workspace_hint = str(resolved.get("workspace_path") or settings.workspace_path)
    package_script_hint = str(resolved.get("package_script") or settings.package_script)
    remote_prefix = f"{project_slug}/releases/__VERSION__"
    release_components = _project_components_to_release_config(project, components)
    release_config = {
        "project": {
            "name": project.name,
            "slug": project_slug,
        },
        "build": {
            "workspace_hint": workspace_hint,
            "artifact_base_url": f"{artifact_public_base_url}/__VERSION__",
            "output_dir": f"dist/releases/__VERSION__",
            "version_strategy": "date-shortsha",
        },
        "components": release_components,
    }
    environment_name = environment.name if environment else "prod"
    webhook_url = environment.webhook_url if environment else "https://deploy-agent.example.com/deploy/hook"
    status_url = environment.status_url if environment else "https://deploy-agent.example.com/deploy/status"
    shared_secret = environment.shared_secret if environment else "replace-with-shared-secret"
    deploy_env = environment.default_environment_name if environment else "prod"
    files = {
        "README.md": _render_starter_readme(
            project=project,
            components=components,
            environment=environment,
            workspace_hint=workspace_hint,
            package_script_hint=package_script_hint,
            lang=lang,
        ),
        "deploy/release.config.json": json.dumps(release_config, ensure_ascii=False, indent=2),
        "deploy/scripts/package_release.sh": _render_build_release_script(),
        "deploy/deploy-agent/Dockerfile": _render_deploy_agent_dockerfile(),
        "deploy/deploy-agent/requirements.txt": _render_deploy_agent_requirements(),
        "deploy/deploy-agent/app.py": _render_deploy_agent_app(lang=lang),
        "deploy/deploy-agent/bin/deploy-release.sh": _render_deploy_release_script(),
        "deploy/deploy-agent/deploy-agent.compose.yml": _render_deploy_agent_compose(project_slug),
        "deploy/deploy-agent/deploy-agent.env.example": _render_deploy_agent_env(
            webhook_url=webhook_url,
            status_url=status_url,
            shared_secret=shared_secret,
            deploy_environment=deploy_env,
            remote_prefix=remote_prefix,
            lang=lang,
        ),
    }
    remote_agent = {
        "webhook_url": webhook_url,
        "status_url": status_url,
        "shared_secret": shared_secret,
        "environment_name": environment_name,
        "deploy_environment": deploy_env,
        "required_contract": {
            "hook_path": "/deploy/hook",
            "status_path": "/deploy/status",
            "hook_payload": {
                "version": "20260327-1200-abcd123",
                "manifest_url": f"{artifact_public_base_url}/20260327-1200-abcd123/manifest.json",
                "triggered_by": "deploybox",
                "commit": "abcd123",
                "environment": deploy_env,
            },
        },
        "steps": _starter_remote_agent_steps(lang),
    }
    return {
        "files": files,
        "release_config": release_config,
        "remote_agent": remote_agent,
        "component_names": [component.name for component in components if component.enabled],
    }


def build_starter_archive(bundle: dict) -> bytes:
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in bundle["files"].items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=f"starter/{name}")
            info.size = len(data)
            if name.endswith(".sh"):
                info.mode = 0o755
            archive.addfile(info, BytesIO(data))
    buffer.seek(0)
    return buffer.read()


def save_project_build_config(
    db: Session,
    *,
    project: Project,
    template_id: int,
    config_override_json: str | None,
) -> ProjectBuildConfig:
    template = db.get(BuildTemplate, template_id)
    if not template:
        raise RuntimeError("构建模板不存在")
    normalized_override = config_override_json.strip() if config_override_json else None
    if normalized_override:
        json.loads(normalized_override)
    build_config = db.scalar(select(ProjectBuildConfig).where(ProjectBuildConfig.project_id == project.id))
    if not build_config:
        build_config = ProjectBuildConfig(project_id=project.id, template_id=template.id)
    build_config.template_id = template.id
    build_config.config_override_json = normalized_override
    db.add(build_config)
    db.commit()
    db.refresh(build_config)
    return build_config


def run_deployment(
    db: Session,
    *,
    project: Project,
    environment: Environment,
    release: Release,
    triggered_by: str | None,
) -> Deployment:
    deployment = Deployment(
        project_id=project.id,
        environment_id=environment.id,
        release_id=release.id,
        status="queued",
        triggered_by=triggered_by,
        progress_percent=5,
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)
    submit_background_job(execute_deployment_job, deployment.id)
    return deployment


def execute_deployment_job(deployment_id: int) -> None:
    db = SessionLocal()
    try:
        deployment = db.execute(
            select(Deployment)
            .options(joinedload(Deployment.project), joinedload(Deployment.environment), joinedload(Deployment.release))
            .where(Deployment.id == deployment_id)
        ).unique().scalar_one_or_none()
        if not deployment:
            return

        adapter = build_adapter(deployment.project.adapter_type, deployment.environment)
        deployment.status = "running"
        deployment.progress_percent = 10
        deployment.started_at = datetime.now(timezone.utc)
        deployment.status_reason = "正在提交发布请求"
        db.commit()

        try:
            result = adapter.trigger_deploy(deployment.release, deployment.triggered_by)
            response_json = result.get("response_json", {})
            deployment.request_payload_json = json.dumps(result.get("request_payload", {}), ensure_ascii=True)
            deployment.adapter_response_json = json.dumps(response_json, ensure_ascii=True)
            deployment.log_excerpt = "\n".join(
                part for part in [response_json.get("stdout", ""), response_json.get("stderr", "")] if part
            )[:4000] or None
            deployment.submitted_at = datetime.now(timezone.utc)
            normalized = _normalize_trigger_response(response_json)
            deployment.status = normalized
            deployment.progress_percent = 30 if normalized not in {"succeeded", "failed"} else 100
            deployment.status_reason = response_json.get("status") or "已提交"
            if normalized == "failed":
                deployment.finished_at = datetime.now(timezone.utc)
                deployment.progress_percent = 100
                db.commit()
                return
            if normalized == "succeeded":
                deployment.finished_at = datetime.now(timezone.utc)
                deployment.progress_percent = 100
                db.commit()
                return
        except httpx.TimeoutException as exc:
            deployment.submitted_at = datetime.now(timezone.utc)
            deployment.status = "timed_out_but_running"
            deployment.progress_percent = 25
            deployment.status_reason = "触发请求超时，改为后台轮询发布状态"
            deployment.adapter_response_json = json.dumps({"detail": str(exc)}, ensure_ascii=True)
            db.commit()
        except httpx.HTTPStatusError as exc:
            deployment.status = "failed"
            deployment.status_reason = f"触发发布失败: HTTP {exc.response.status_code}"
            deployment.adapter_response_json = exc.response.text
            deployment.log_excerpt = exc.response.text[:4000]
            deployment.finished_at = datetime.now(timezone.utc)
            deployment.progress_percent = 100
            db.commit()
            return
        except Exception as exc:  # pragma: no cover
            deployment.status = "failed"
            deployment.status_reason = str(exc)
            deployment.adapter_response_json = str(exc)
            deployment.log_excerpt = str(exc)[:4000]
            deployment.finished_at = datetime.now(timezone.utc)
            deployment.progress_percent = 100
            db.commit()
            return

        _watch_deployment_until_terminal(db, deployment, adapter)
    finally:
        db.close()


def refresh_deployment_status(deployment: Deployment, db: Session) -> Deployment:
    adapter = build_adapter(deployment.project.adapter_type, deployment.environment)
    status_payload = adapter.fetch_status()
    deployment.last_status_json = json.dumps(status_payload, ensure_ascii=True)
    deployment.last_polled_at = datetime.now(timezone.utc)
    normalized = _normalize_status_payload(status_payload, deployment.release.version)
    if normalized:
        deployment.status = normalized
        if normalized in TERMINAL_DEPLOYMENT_STATUSES:
            deployment.finished_at = datetime.now(timezone.utc)
            deployment.progress_percent = 100
    db.commit()
    db.refresh(deployment)
    return deployment


def create_build_job(
    db: Session,
    *,
    project: Project,
    triggered_by: str | None,
    payload_json: str | None = None,
    artifact_mode: str = "auto",
    environment_id: int | None = None,
    selected_component_ids: list[int] | None = None,
) -> BuildJob:
    build_config = ensure_project_build_config(db, project)
    components = list_project_components(db, project)
    selected_components = _selected_project_components(components, selected_component_ids)
    normalized_payload = _normalize_build_payload(payload_json, selected_components)
    job = BuildJob(
        project_id=project.id,
        template_id=build_config.template_id,
        environment_id=environment_id,
        status="queued",
        artifact_mode=artifact_mode,
        source_type="generated",
        progress_percent=5,
        current_stage="queued",
        payload_json=normalized_payload,
        triggered_by=triggered_by,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    _record_build_event(db, job, "info", "queued", "构建任务已创建", 5)
    _record_build_event(
        db,
        job,
        "info",
        "queued",
        f"本次将处理组件: {', '.join(component.name for component in selected_components)}",
        5,
    )
    submit_background_job(execute_build_job, job.id)
    return job


def execute_build_job(build_job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.execute(
            select(BuildJob)
            .options(joinedload(BuildJob.project), joinedload(BuildJob.template), joinedload(BuildJob.environment))
            .where(BuildJob.id == build_job_id)
        ).unique().scalar_one_or_none()
        if not job:
            return

        project = job.project
        project_components = list_project_components(db, project)
        build_config = db.execute(
            select(ProjectBuildConfig)
            .options(joinedload(ProjectBuildConfig.template))
            .where(ProjectBuildConfig.project_id == project.id)
        ).unique().scalar_one_or_none()
        if not build_config:
            build_config = ensure_project_build_config(db, project)
            db.refresh(build_config)
        template = build_config.template or job.template
        if not template:
            raise RuntimeError("项目尚未绑定构建模板")

        job.template_id = template.id
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        job.current_stage = "prepare"
        job.progress_percent = 10
        db.commit()
        _record_build_event(db, job, "info", "prepare", f"使用模板 {template.name}", 10)

        if template.strategy != "manifest_script_v1":
            raise RuntimeError(f"当前仅支持 manifest_script_v1，实际为 {template.strategy}")

        release = _run_manifest_script_build(db, job, project, build_config, project_components)

        job.status = "succeeded"
        job.current_stage = "completed"
        job.progress_percent = 100
        job.output_version = release.version
        job.manifest_url = release.manifest_url
        job.finished_at = datetime.now(timezone.utc)
        job.result_json = json.dumps(
            {
                "release_id": release.id,
                "version": release.version,
                "manifest_url": release.manifest_url,
                "storage_mode": release.storage_mode,
            },
            ensure_ascii=True,
        )
        _record_build_event(db, job, "success", "completed", f"Release {release.version} 已生成", 100)
        db.commit()

        if job.environment_id:
            environment = db.get(Environment, job.environment_id)
            if environment:
                deployment = run_deployment(
                    db,
                    project=project,
                    environment=environment,
                    release=release,
                    triggered_by=f"{job.triggered_by or 'manual'} (build)",
                )
                job.result_json = json.dumps(
                    {
                        "release_id": release.id,
                        "version": release.version,
                        "manifest_url": release.manifest_url,
                        "storage_mode": release.storage_mode,
                        "deployment_id": deployment.id,
                    },
                    ensure_ascii=True,
                )
                db.commit()
    except Exception as exc:  # pragma: no cover
        job = db.get(BuildJob, build_job_id)
        if job:
            job.status = "failed"
            job.current_stage = "failed"
            job.progress_percent = 100
            job.error_message = str(exc)
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
            _record_build_event(db, job, "error", "failed", str(exc), 100)
    finally:
        db.close()


def sync_release_manifest(db: Session, release: Release) -> Release:
    settings = get_settings()
    try:
        with httpx.Client(timeout=settings.request_timeout_seconds) as client:
            response = client.get(release.manifest_url)
        response.raise_for_status()
        payload = response.json()
        _apply_release_manifest_payload(release, payload)
    except Exception as exc:  # pragma: no cover - network/runtime defensive path
        release.components.clear()
        release.component_count = 0
        release.manifest_sync_status = "failed"
        release.manifest_sync_error = str(exc)
    db.add(release)
    db.commit()
    db.refresh(release)
    return release


def sync_release_manifest_payload(db: Session, release: Release, payload: dict) -> Release:
    try:
        _apply_release_manifest_payload(release, payload)
    except Exception as exc:
        release.components.clear()
        release.component_count = 0
        release.manifest_sync_status = "failed"
        release.manifest_sync_error = str(exc)
    db.add(release)
    db.commit()
    db.refresh(release)
    return release


def _record_build_event(
    db: Session,
    build_job: BuildJob,
    event_type: str,
    stage: str,
    message: str,
    progress_percent: int | None = None,
) -> None:
    db.add(
        BuildJobEvent(
            build_job_id=build_job.id,
            event_type=event_type,
            stage=stage,
            message=message,
            progress_percent=progress_percent,
        )
    )
    build_job.current_stage = stage
    if progress_percent is not None:
        build_job.progress_percent = progress_percent
    db.add(build_job)
    db.commit()


def _watch_deployment_until_terminal(db: Session, deployment: Deployment, adapter) -> None:
    settings = get_settings()
    started = time.time()
    while time.time() - started < settings.deployment_watch_timeout_seconds:
        try:
            payload = adapter.fetch_status()
            deployment.last_status_json = json.dumps(payload, ensure_ascii=True)
            deployment.last_polled_at = datetime.now(timezone.utc)
            normalized = _normalize_status_payload(payload, deployment.release.version)
            if normalized:
                deployment.status = normalized
                deployment.status_reason = payload.get("status") or deployment.status_reason
                if normalized in {"succeeded", "failed"}:
                    deployment.progress_percent = 100
                    deployment.finished_at = datetime.now(timezone.utc)
                    db.commit()
                    return
                deployment.progress_percent = max(deployment.progress_percent, 65)
            else:
                deployment.status = "running"
                deployment.progress_percent = max(deployment.progress_percent, 50)
            db.commit()
        except Exception as exc:  # pragma: no cover
            deployment.status_reason = f"轮询状态失败: {exc}"
            db.commit()
        time.sleep(settings.deployment_poll_interval_seconds)

    deployment.status = "timed_out_but_running"
    deployment.status_reason = "长时间未能确认最终状态，请继续手动刷新"
    deployment.finished_at = datetime.now(timezone.utc)
    deployment.progress_percent = max(deployment.progress_percent, 90)
    db.commit()


def _normalize_trigger_response(response_json: dict) -> str:
    status = str(response_json.get("status") or "").strip().lower()
    if status in SUCCESS_AGENT_STATUSES:
        return "succeeded"
    if status in FAILED_AGENT_STATUSES:
        return "failed"
    return "submitted"


def _normalize_status_payload(payload: dict, release_version: str) -> str | None:
    status = str(payload.get("status") or "").strip().lower()
    version = str(payload.get("version") or "").strip()
    if version != release_version:
        return None
    if status in SUCCESS_AGENT_STATUSES:
        return "succeeded"
    if status in FAILED_AGENT_STATUSES:
        return "failed"
    return "running"


def _run_manifest_script_build(
    db: Session,
    job: BuildJob,
    project: Project,
    build_config: ProjectBuildConfig,
    project_components: list[ProjectComponent],
) -> Release:
    settings = get_settings()
    resolved = _resolve_build_config(settings, build_config)
    resolved_components = _project_components_to_release_config(project, project_components)
    if not resolved_components:
        raise RuntimeError("当前项目还没有可用组件，请先在接入配置里维护组件")
    selected_components = _selected_project_components(
        project_components,
        _extract_selected_component_ids(job.payload_json),
    )
    selected_names = {component.name for component in selected_components}
    resolved["components"] = [item for item in resolved_components if str(item.get("name")) in selected_names]
    if not resolved["components"]:
        raise RuntimeError("本次构建没有选中任何可发布组件")
    workspace = Path(resolved["workspace_path"]).resolve()
    package_script = (workspace / resolved["package_script"]).resolve()
    artifact_base_url = str(resolved["artifact_public_base_url"]).rstrip("/")
    if not workspace.exists():
        raise RuntimeError(f"工作目录不存在: {workspace}")
    if not package_script.exists():
        raise RuntimeError(f"打包脚本不存在: {package_script}")

    use_oss = settings.use_oss
    normalized_mode = (job.artifact_mode or "auto").strip().lower()
    if normalized_mode == "oss":
        use_oss = True
    elif normalized_mode == "local":
        use_oss = False
    elif normalized_mode != "auto":
        raise RuntimeError("不支持的制品模式，只能是 auto、oss 或 local")

    env = os.environ.copy()
    env["ARTIFACT_BASE_URL"] = artifact_base_url
    local_release_root = Path(settings.local_artifacts_path).resolve()
    if not use_oss:
        local_output_dir = local_release_root / str(job.id)
        env["OUTPUT_DIR"] = str(local_output_dir)
    for key, value in (resolved.get("extra_env") or {}).items():
        env[str(key)] = str(value)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=".deploy-console.release.config.",
        suffix=".json",
        dir=workspace,
        delete=False,
    ) as temp_file:
        temp_file.write(json.dumps(resolved, ensure_ascii=False, indent=2))
        temp_config_path = Path(temp_file.name)
    env["CONFIG_FILE"] = str(temp_config_path)

    _record_build_event(db, job, "info", "package", "开始执行打包脚本", 20)
    output_lines: list[str] = []
    combined_stdout = ""
    component_total = len(resolved["components"])
    completed_components = 0
    active_progress = 20
    try:
        process = subprocess.Popen(
            ["bash", str(package_script)],
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            output_lines.append(line)
            if line.startswith("==> building "):
                component_name = line.removeprefix("==> building ").split(" (", 1)[0].strip()
                active_progress = 20 + int(25 * completed_components / max(component_total, 1))
                _record_build_event(
                    db,
                    job,
                    "info",
                    "package",
                    f"正在构建组件 {completed_components + 1}/{component_total}: {component_name}",
                    active_progress,
                )
            elif line.startswith("==> saving "):
                completed_components += 1
                active_progress = 20 + int(25 * completed_components / max(component_total, 1))
                _record_build_event(
                    db,
                    job,
                    "info",
                    "package",
                    f"已完成组件 {completed_components}/{component_total}: {line.removeprefix('==> saving ').strip()}",
                    active_progress,
                )
            elif line.startswith("==> pulling external image for "):
                completed_components += 1
                component_name = line.removeprefix("==> pulling external image for ").split(" (", 1)[0].strip()
                active_progress = 20 + int(25 * completed_components / max(component_total, 1))
                _record_build_event(
                    db,
                    job,
                    "info",
                    "package",
                    f"已完成外部镜像 {completed_components}/{component_total}: {component_name}",
                    active_progress,
                )
        process.wait()
    finally:
        if temp_config_path.exists():
            temp_config_path.unlink()
    combined_stdout = "\n".join(output_lines).strip()
    job.log_excerpt = _tail_text(output_lines)
    db.commit()
    if process.returncode != 0:
        error_text = (combined_stdout or "package_release failed").strip()
        raise RuntimeError(error_text[:4000])

    version = _parse_stdout_value(combined_stdout, "Version:")
    output_dir = _parse_stdout_value(combined_stdout, "Release package created at")
    if not version or not output_dir:
        raise RuntimeError("打包脚本输出缺少 Version 或 Release package created at")
    output_path = Path(output_dir).resolve()
    if not use_oss:
        final_output_path = local_release_root / version
        if output_path != final_output_path:
            final_output_path.parent.mkdir(parents=True, exist_ok=True)
            if final_output_path.exists():
                shutil.rmtree(final_output_path)
            shutil.move(str(output_path), str(final_output_path))
            output_path = final_output_path
    manifest_path = output_path / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"未找到 manifest 文件: {manifest_path}")

    _record_build_event(db, job, "info", "manifest", "正在解析 manifest", 55)
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["version"] = version

    storage_mode = "oss" if use_oss else "local"
    remote_prefix = f"{project.slug}/releases/{version}"
    if use_oss:
        _record_build_event(db, job, "info", "upload", "开始上传到 OSS", 70)
        storage = build_artifact_storage(settings)
        manifest_payload["artifact_storage"] = build_oss_storage_descriptor(settings)
        components = _validated_components(manifest_payload)
        total = len(components)
        for index, component in enumerate(components, start=1):
            image_tar_url = str(component.get("image_tar_url") or "").strip()
            if not image_tar_url:
                progress = 70 + int(20 * index / max(total, 1))
                _record_build_event(
                    db,
                    job,
                    "info",
                    "upload",
                    f"组件 {component.get('name') or index} 为外部镜像，跳过 tar 上传",
                    progress,
                )
                continue
            artifact_name = Path(image_tar_url).name
            artifact_path = output_path / artifact_name
            if not artifact_name or not artifact_path.exists():
                raise RuntimeError(f"未找到组件文件: {artifact_name or '-'}")
            remote_object_key = f"{remote_prefix}/{artifact_name}"
            component["image_tar_url"] = storage.upload_bytes(
                data=artifact_path.read_bytes(),
                remote_path=remote_object_key,
                content_type="application/x-tar",
            )
            component["image_tar_object_key"] = remote_object_key
            progress = 70 + int(20 * index / max(total, 1))
            _record_build_event(
                db,
                job,
                "info",
                "upload",
                f"已上传组件 {index}/{total}: {artifact_name}",
                progress,
            )

        sha_file = output_path / "sha256sum.txt"
        if sha_file.exists():
            storage.upload_bytes(
                data=sha_file.read_bytes(),
                remote_path=f"{remote_prefix}/sha256sum.txt",
                content_type="text/plain; charset=utf-8",
            )

        manifest_url = storage.upload_bytes(
            data=json.dumps(manifest_payload, ensure_ascii=True, indent=2).encode("utf-8"),
            remote_path=f"{remote_prefix}/manifest.json",
            content_type="application/json; charset=utf-8",
        )
    else:
        _record_build_event(db, job, "info", "local_artifacts", "使用本地制品站登记 release", 75)
        manifest_payload.pop("artifact_storage", None)
        manifest_payload["components"] = _validated_components(manifest_payload)
        manifest_url = f"{artifact_base_url}/{version}/manifest.json"
        for component in manifest_payload["components"]:
            image_tar_url = str(component.get("image_tar_url") or "").strip()
            if not image_tar_url:
                component.pop("image_tar_object_key", None)
                continue
            artifact_name = Path(image_tar_url).name
            component["image_tar_url"] = f"{artifact_base_url}/{version}/{artifact_name}"
            component.pop("image_tar_object_key", None)
        manifest_path.write_text(
            json.dumps(manifest_payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    _record_build_event(db, job, "info", "register", "登记 release 并同步组件", 92)
    release = _create_release_from_manifest(
        db,
        project=project,
        version=version,
        manifest_url=manifest_url,
        manifest_payload=manifest_payload,
        commit=_git_commit(workspace),
        payload_json=job.payload_json,
        created_by=job.triggered_by,
        source_type="generated",
        storage_mode=storage_mode,
    )
    job.output_version = release.version
    job.manifest_url = release.manifest_url
    job.storage_mode = storage_mode
    db.commit()
    return release


def _resolve_build_config(settings, build_config: ProjectBuildConfig) -> dict:
    template_config = _parse_json_object(build_config.template.config_json if build_config.template else None)
    override_config = _parse_json_object(build_config.config_override_json)
    workspace_path = resolve_project_workspace(build_config.project, settings) if build_config.project else Path(settings.workspace_path).resolve()
    default_workspace_root = Path(settings.workspace_path).resolve()
    raw_override_workspace = str(override_config.get("workspace_path") or "").strip()
    if raw_override_workspace and Path(raw_override_workspace).resolve() == default_workspace_root:
        override_config.pop("workspace_path", None)
    resolved = {
        "workspace_path": str(workspace_path),
        "package_script": settings.package_script,
        "artifact_public_base_url": settings.package_artifact_public_base_url or settings.package_artifact_base_url,
        "extra_env": {},
    }
    resolved.update(template_config)
    resolved.update(override_config)
    return resolved


def _parse_json_object(raw: str | None) -> dict:
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("构建配置必须是 JSON 对象")
    return value


def normalize_build_config_override(raw: str | None, settings=None) -> dict:
    settings = settings or get_settings()
    value = _parse_json_object(raw)
    normalized = dict(value)
    raw_workspace = str(normalized.get("workspace_path") or "").strip()
    if raw_workspace:
        default_workspace_root = Path(settings.workspace_path).resolve()
        if Path(raw_workspace).resolve() == default_workspace_root:
            normalized.pop("workspace_path", None)
    if normalized.get("package_script") == settings.package_script:
        # Keep the override payload focused on true deviations from defaults.
        normalized.pop("package_script", None)
    return normalized


def _normalize_build_payload(payload_json: str | None, selected_components: list[ProjectComponent]) -> str:
    base_payload: dict = {}
    if payload_json and payload_json.strip():
        raw = payload_json.strip()
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                base_payload = parsed
            else:
                base_payload = {"note": raw}
        except json.JSONDecodeError:
            base_payload = {"note": raw}
    base_payload["selected_component_ids"] = [component.id for component in selected_components]
    base_payload["selected_components"] = [component.name for component in selected_components]
    return json.dumps(base_payload, ensure_ascii=True) if base_payload else ""


def _extract_selected_component_ids(payload_json: str | None) -> list[int] | None:
    payload = _parse_json_object(payload_json) if payload_json else {}
    component_ids = payload.get("selected_component_ids")
    if isinstance(component_ids, list):
        values = [int(item) for item in component_ids if str(item).isdigit()]
        return values or None
    return None


def _selected_project_components(
    components: list[ProjectComponent],
    selected_component_ids: list[int] | None,
) -> list[ProjectComponent]:
    enabled_components = [component for component in components if component.enabled]
    if not enabled_components:
        raise RuntimeError("当前项目没有启用中的组件，请先维护组件配置")
    if selected_component_ids:
        selected_id_set = {int(item) for item in selected_component_ids}
        selected = [component for component in enabled_components if component.id in selected_id_set]
    else:
        selected = [component for component in enabled_components if component.default_selected]
        if not selected:
            selected = enabled_components
    if not selected:
        raise RuntimeError("本次没有可构建组件，请至少选择一个启用中的组件")
    return selected


def _project_components_to_release_config(project: Project, components: list[ProjectComponent]) -> list[dict]:
    items = []
    for component in components:
        if not component.enabled:
            continue
        items.append(
            {
                "name": component.name,
                "image": _unwrap_compose_image_reference(component.image),
                "dockerfile": component.dockerfile,
                "context": component.context_path,
                "service": component.service_name,
                "tar_name": component.tar_name_pattern or f"{project.slug}-{component.service_name}-__VERSION__.tar",
                "build_enabled": component.build_enabled,
            }
        )
    return items


def _create_release_from_manifest(
    db: Session,
    *,
    project: Project,
    version: str,
    manifest_url: str,
    manifest_payload: dict,
    commit: str | None,
    payload_json: str | None,
    created_by: str | None,
    source_type: str,
    storage_mode: str,
) -> Release:
    existing = db.scalar(select(Release).where(Release.project_id == project.id, Release.version == version))
    if existing:
        raise RuntimeError(f"版本 {version} 已存在，请更换版本号后重试")
    release = Release(
        project_id=project.id,
        version=version,
        manifest_url=manifest_url,
        commit=commit,
        payload_json=payload_json,
        created_by=created_by,
        source_type=source_type,
        storage_mode=storage_mode,
    )
    db.add(release)
    db.commit()
    db.refresh(release)
    sync_release_manifest_payload(db, release, manifest_payload)
    return release


def _apply_release_manifest_payload(release: Release, payload: dict) -> None:
    components = payload.get("components") or []
    if not isinstance(components, list):
        raise ValueError("manifest components must be a list")
    release.components.clear()
    release.component_count = 0
    release.manifest_sync_error = None
    release.manifest_json = json.dumps(payload, ensure_ascii=True)
    release.manifest_sync_status = "synced"
    for index, component in enumerate(components):
        release.components.append(
            ReleaseComponent(
                name=str(component.get("name", f"component-{index}")),
                image=str(component.get("image", "")),
                image_tar_url=component.get("image_tar_url"),
                image_tar_sha256=component.get("image_tar_sha256"),
                position=index,
            )
        )
    release.component_count = len(release.components)


def _parse_stdout_value(stdout: str, prefix: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return None


def _tail_text(lines: list[str], max_chars: int = 4000) -> str | None:
    if not lines:
        return None
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _git_commit(workspace: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _validated_components(manifest_payload: dict) -> list[dict]:
    components = manifest_payload.get("components")
    if not isinstance(components, list) or not components:
        raise RuntimeError("manifest 中缺少有效 components")
    return components


def _parse_compose_services(raw: str) -> list[dict]:
    document = yaml.safe_load(raw) or {}
    if not isinstance(document, dict):
        return []
    services_node = document.get("services") or {}
    if not isinstance(services_node, dict):
        return []
    services: list[dict] = []
    for service_name, config in services_node.items():
        if not isinstance(config, dict):
            config = {}
        image = str(config.get("image") or "").strip()
        build = config.get("build")
        context_path = "."
        dockerfile = "./Dockerfile"
        has_build = False
        if isinstance(build, str):
            context_path = build.strip() or "."
            has_build = True
        elif isinstance(build, dict):
            context_path = str(build.get("context") or ".").strip() or "."
            dockerfile = str(build.get("dockerfile") or "./Dockerfile").strip() or "./Dockerfile"
            has_build = True
        elif build:
            has_build = True
        services.append(
            {
                "service_name": str(service_name).strip(),
                "image": image,
                "context_path": context_path,
                "dockerfile": dockerfile,
                "has_build": has_build,
                "config": config,
            }
        )
    return services


def _service_image_env_key(service_name: str) -> str:
    return "DEPLOY_IMAGE_" + "".join(ch if ch.isalnum() else "_" for ch in service_name).upper()


def _unwrap_compose_image_reference(image: str) -> str:
    value = (image or "").strip()
    if not value:
        return ""
    match = re.fullmatch(r"\$\{[^}:]+(?::-|:-)([^}]+)\}", value)
    if match:
        return match.group(1).strip()
    return value


def _release_image_fallback(image: str) -> str:
    value = _unwrap_compose_image_reference(image)
    if not value:
        return ""
    if value.endswith(":__VERSION__"):
        return value[: -len("__VERSION__")] + "latest"
    return value


def _recommended_service_stub(component: ProjectComponent) -> dict:
    service = {
        "image": f"${{{_service_image_env_key(component.service_name)}:-{_release_image_fallback(component.image)}}}",
    }
    if component.build_enabled:
        # Preserve build-related hints in the recommended compose so users can
        # see where this service should come from, even though release-time
        # deployment should consume image tags instead of rebuilding.
        service["pull_policy"] = "never"
    return service


def analyze_compose_release_readiness(
    project: Project,
    project_components: list[ProjectComponent],
    compose_path: Path,
) -> dict:
    raw = compose_path.read_text(encoding="utf-8")
    parsed_services = _parse_compose_services(raw)
    document = yaml.safe_load(raw) or {}
    if not isinstance(document, dict):
        raise RuntimeError("Compose 文件格式无效，根节点必须是对象")
    services_node = document.get("services") or {}
    if not isinstance(services_node, dict):
        raise RuntimeError("Compose 文件缺少 services 配置")
    parsed_by_name = {str(item["service_name"]): item for item in parsed_services}
    service_order = list(services_node.keys())
    component_by_service = {component.service_name: component for component in project_components if component.enabled}
    issues: list[dict[str, str]] = []
    recommended_document = deepcopy(document)
    recommended_document.pop("version", None)
    recommended_services = recommended_document.get("services")
    if not isinstance(recommended_services, dict):
        recommended_services = {}
        recommended_document["services"] = recommended_services

    if "version" in document:
        issues.append(
            {
                "severity": "warning",
                "summary": "顶层 version 字段已过时",
                "detail": "Docker Compose v2 会忽略它，建议直接删除，避免误解。",
            }
        )

    for service_name in service_order:
        item = parsed_by_name.get(
            service_name,
            {
                "service_name": service_name,
                "image": "",
                "context_path": ".",
                "dockerfile": "./Dockerfile",
                "has_build": False,
            },
        )
        component = component_by_service.get(service_name)
        original_config = item.get("config") if isinstance(item.get("config"), dict) else {}
        service_config = recommended_services.get(service_name)
        if not isinstance(service_config, dict):
            service_config = {}
            recommended_services[service_name] = service_config
        has_build = bool(item.get("has_build"))
        original_image = str(item.get("image") or "").strip()
        env_key = _service_image_env_key(service_name)
        fallback_image = _release_image_fallback(
            component.image if component else (original_image or _default_component_image(project, service_name))
        )
        service_config["image"] = f"${{{env_key}:-{fallback_image}}}"
        if "build" in service_config and (component or has_build):
            service_config.pop("build", None)

        if component and has_build:
            issues.append(
                {
                    "severity": "warning",
                    "summary": f"service `{service_name}` 同时存在 build 配置",
                    "detail": "发布态建议以 image 为准，避免 docker compose 在目标机上重新构建本地镜像。",
                }
            )
        elif has_build and not original_image:
            issues.append(
                {
                    "severity": "warning",
                    "summary": f"service `{service_name}` 只有 build 没有 image",
                    "detail": "这会导致部署后很难确认容器是否真的切换到本次 release 镜像。",
                }
            )

        if original_image.endswith(":latest"):
            issues.append(
                {
                    "severity": "warning",
                    "summary": f"service `{service_name}` 使用 latest 标签",
                    "detail": "latest 不利于追踪版本，建议使用固定版本或由 DEPLOY_IMAGE_* 注入。",
                }
            )

        if component and "${" + env_key not in str(original_config.get("image") or ""):
            issues.append(
                {
                    "severity": "info",
                    "summary": f"service `{service_name}` 未接入 {env_key}",
                    "detail": "建议改成 image: ${DEPLOY_IMAGE_<SERVICE>:-默认镜像}，这样发布时才能稳定切换到新镜像。",
                }
            )

        volumes = service_config.get("volumes")
        if component and component.build_enabled and isinstance(volumes, list):
            filtered_volumes = []
            normalized_context = (component.context_path or "").strip()
            context_candidates = {
                normalized_context,
                normalized_context.lstrip("./"),
                f"./{normalized_context.lstrip('./')}",
            }
            for volume_item in volumes:
                raw_mount = str(volume_item).strip()
                mount_source = raw_mount.split(":", 1)[0].strip()
                if mount_source in context_candidates:
                    issues.append(
                        {
                            "severity": "warning",
                            "summary": f"service `{service_name}` 存在源码目录挂载",
                            "detail": f"`{raw_mount}` 会覆盖镜像内代码，发布后即使切换 tag 也可能看不到效果。",
                        }
                    )
                    continue
                filtered_volumes.append(volume_item)
            service_config["volumes"] = filtered_volumes

    # Hand-added enabled components should also appear in the recommended
    # compose, otherwise users see a stale suggestion after changing component
    # configuration in DeployBox.
    for component in project_components:
        if not component.enabled or component.service_name in recommended_services:
            continue
        recommended_services[component.service_name] = _recommended_service_stub(component)
        issues.append(
            {
                "severity": "info",
                "summary": f"已为组件 `{component.service_name}` 追加推荐 service",
                "detail": "该组件存在于 DeployBox 配置中，但当前 compose 文件里还没有对应 service，推荐文件已补上最小定义。",
            }
        )

    if not issues:
        issues.append(
            {
                "severity": "success",
                "summary": "当前 compose 基本符合发布接入要求",
                "detail": "仍建议保留固定镜像版本或 DEPLOY_IMAGE_* 注入，以方便追踪实际运行镜像。",
            }
        )

    return {
        "compose_path": str(compose_path),
        "issues": issues,
        "recommended_compose": yaml.safe_dump(recommended_document, sort_keys=False, allow_unicode=True),
        "service_count": len(parsed_services),
    }


def _normalize_compose_dockerfile_path(dockerfile: str, context_path: str) -> str:
    raw_dockerfile = (dockerfile or "./Dockerfile").strip()
    raw_context = (context_path or ".").strip()
    if Path(raw_dockerfile).is_absolute():
        return raw_dockerfile
    if raw_context in {"", "."}:
        return raw_dockerfile
    if raw_dockerfile.startswith("./"):
        raw_dockerfile = raw_dockerfile[2:]
    return f"{raw_context.rstrip('/')}/{raw_dockerfile}"


def _default_component_image(project: Project, service_name: str) -> str:
    prefix = (project.image_registry_prefix or "").strip().rstrip("/")
    if prefix:
        return f"{prefix}/{project.slug}-{service_name}:__VERSION__"
    return f"{project.slug}-{service_name}:__VERSION__"


def _is_infra_component(service_name: str, image: str) -> bool:
    probe = f"{service_name} {image}".lower()
    infra_keywords = [
        "postgres",
        "mysql",
        "mariadb",
        "redis",
        "mongo",
        "rabbitmq",
        "kafka",
        "zookeeper",
        "minio",
        "grafana",
        "prometheus",
        "elasticsearch",
        "kibana",
        "nginx",
    ]
    return any(keyword in probe for keyword in infra_keywords)


def _render_build_release_script() -> str:
    return dedent(
        """\
        #!/usr/bin/env bash
        set -euo pipefail

        ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        PROJECT_ROOT="$(cd "${ROOT_DIR}/../.." && pwd)"
        CONFIG_FILE="${CONFIG_FILE:-${1:-${ROOT_DIR}/../release.config.json}}"
        if [[ ! -f "${CONFIG_FILE}" ]]; then
          echo "release config not found: ${CONFIG_FILE}" >&2
          exit 1
        fi

        short_sha="$(git -C "${PROJECT_ROOT}" rev-parse --short HEAD 2>/dev/null || true)"
        timestamp="$(date +%Y%m%d-%H%M)"
        if [[ -n "${short_sha}" ]]; then
          VERSION="${VERSION:-${timestamp}-${short_sha}}"
        else
          VERSION="${VERSION:-${timestamp}}"
        fi

        export CONFIG_FILE ROOT_DIR PROJECT_ROOT VERSION

        ARTIFACT_BASE_URL="${ARTIFACT_BASE_URL:-$(python3 - <<'PY'
        import json, os
        from pathlib import Path
        config = json.loads(Path(os.environ["CONFIG_FILE"]).read_text(encoding="utf-8"))
        print(str(config.get("build", {}).get("artifact_base_url", "https://placeholder.invalid/releases/__VERSION__")).replace("__VERSION__", os.environ["VERSION"]))
        PY
        )}"
        OUTPUT_DIR="${OUTPUT_DIR:-${PROJECT_ROOT}/dist/releases/${VERSION}}"
        mkdir -p "${OUTPUT_DIR}"
        COMPONENTS_FILE="${OUTPUT_DIR}/components.jsonl"
        export ARTIFACT_BASE_URL OUTPUT_DIR COMPONENTS_FILE
        : > "${COMPONENTS_FILE}"

        while IFS=$'\\t' read -r name service image dockerfile context tar_name build_enabled; do
          if [[ "${build_enabled}" == "1" ]]; then
            if [[ "${dockerfile}" = /* ]]; then
              dockerfile_path="${dockerfile}"
            else
              dockerfile_path="${PROJECT_ROOT}/${dockerfile}"
            fi
            if [[ "${context}" = /* ]]; then
              context_path="${context}"
            else
              context_path="${PROJECT_ROOT}/${context}"
            fi
            echo "==> building ${name} (${image})"
            docker build -t "${image}" -f "${dockerfile_path}" "${context_path}"
            echo "==> saving ${tar_name}"
            docker save -o "${OUTPUT_DIR}/${tar_name}" "${image}"
            sha="$(shasum -a 256 "${OUTPUT_DIR}/${tar_name}" | awk '{print $1}')"
          else
            echo "==> pulling external image for ${name} (${image})"
            docker pull "${image}"
            echo "==> saving ${tar_name}"
            docker save -o "${OUTPUT_DIR}/${tar_name}" "${image}"
            sha="$(shasum -a 256 "${OUTPUT_DIR}/${tar_name}" | awk '{print $1}')"
          fi
          name="${name}" service="${service}" image="${image}" dockerfile="${dockerfile}" context="${context}" tar_name="${tar_name}" sha="${sha}" build_enabled="${build_enabled}" COMPONENTS_FILE="${COMPONENTS_FILE}" python3 - <<'PY'
        import json, os
        from pathlib import Path
        record = {
            "name": os.environ["name"],
            "service": os.environ["service"],
            "image": os.environ["image"],
            "dockerfile": os.environ["dockerfile"],
            "context": os.environ["context"],
            "tar_name": os.environ["tar_name"],
            "sha": os.environ["sha"],
            "build_enabled": os.environ["build_enabled"] == "1",
        }
        with Path(os.environ["COMPONENTS_FILE"]).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\\n")
        PY
        done < <(
          python3 - <<'PY'
        import json, os, re
        from pathlib import Path
        def unwrap_image_ref(value: str) -> str:
            raw = str(value or "").strip()
            if not raw:
                return ""
            match = re.fullmatch(r"\\$\\{[^}:]+(?::-|:-)([^}]+)\\}", raw)
            if match:
                return match.group(1).strip()
            return raw
        config = json.loads(Path(os.environ["CONFIG_FILE"]).read_text(encoding="utf-8"))
        version = os.environ["VERSION"]
        for item in config.get("components", []):
            name = item["name"]
            service = item.get("service") or name
            image = unwrap_image_ref(item["image"]).replace("__VERSION__", version)
            dockerfile = item.get("dockerfile", "./Dockerfile")
            context = item.get("context", ".")
            tar_name = str(item.get("tar_name") or f"{name}-__VERSION__.tar").replace("__VERSION__", version)
            build_enabled = "1" if item.get("build_enabled", True) else "0"
            print("\\t".join([name, service, image, dockerfile, context, tar_name, build_enabled]))
        PY
        )

        python3 - <<'PY'
        import json, os
        from pathlib import Path

        version = os.environ["VERSION"]
        output_dir = Path(os.environ["OUTPUT_DIR"])
        artifact_base_url = os.environ["ARTIFACT_BASE_URL"].rstrip("/")
        items = []
        for line in Path(os.environ["COMPONENTS_FILE"]).read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            items.append(
                {
                    "name": record["name"],
                    "service": record["service"],
                    "image": record["image"],
                    "image_tar_url": f"{artifact_base_url}/{record['tar_name']}" if record["tar_name"] else None,
                    "image_tar_sha256": record["sha"] or None,
                }
            )
        manifest = {"version": version, "components": items}
        (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        sha_lines = []
        for item in items:
            if item["image_tar_sha256"] and item["image_tar_url"]:
                sha_lines.append(f"{item['image_tar_sha256']}  {Path(item['image_tar_url']).name}")
        if sha_lines:
            (output_dir / "sha256sum.txt").write_text("\\n".join(sha_lines) + "\\n", encoding="utf-8")
        PY

        echo "Version: ${VERSION}"
        echo "Release package created at: ${OUTPUT_DIR}"
        """
    )


def _render_starter_readme(
    *,
    project: Project,
    components: list[ProjectComponent],
    environment: Environment | None,
    workspace_hint: str,
    package_script_hint: str,
    lang: str = "zh",
) -> str:
    environment_name = environment.name if environment else "prod"
    webhook_url = environment.webhook_url if environment else "https://deploy-agent.example.com/deploy/hook"
    status_url = environment.status_url if environment else "https://deploy-agent.example.com/deploy/status"
    if lang == "en":
        component_lines = "\n".join(
            f"- `{component.name}` -> service=`{component.service_name}` image=`{component.image}` mode=`{'docker_build' if component.build_enabled else 'external_image'}`"
            for component in components
            if component.enabled
        ) or "- No enabled components yet. Maintain the component list in DeployBox first."
        return dedent(
            f"""\
            # {project.name} Starter Kit

            This starter package helps a Docker / Docker Compose project onboard into DeployBox. Define which components are deployable first, then complete the flow from release generation to remote deploy-agent rollout.

            ## Component Model

            __COMPONENT_LINES__
            - Workspace hint: {workspace_hint}
            - Default package script hint: {package_script_hint}

            ## Values Generated From Current Project Config

            The following values come directly from the current project config in DeployBox:

            - Project name: {project.name}
            - Project slug: {project.slug}
            - Workspace hint: {workspace_hint}
            - Component list, service names, image names, Dockerfile, context, and build mode
            - Webhook URL: {webhook_url}
            - Status URL: {status_url}
            - Logical environment name: {environment.default_environment_name if environment else 'prod'}

            If any of these values do not match your project, go back to DeployBox, fix the project config, and download the starter again.

            ## Platform Defaults

            - Version strategy: `date-shortsha`
            - Artifact output directory: `dist/releases/<version>`
            - Default deploy-agent port: `9000`
            - Minimal deploy-agent implementation and compose example

            ## Starter Layout

            - `README.md`
            - `deploy/release.config.json`
            - `deploy/scripts/package_release.sh`
            - `deploy/deploy-agent/Dockerfile`
            - `deploy/deploy-agent/requirements.txt`
            - `deploy/deploy-agent/app.py`
            - `deploy/deploy-agent/bin/deploy-release.sh`
            - `deploy/deploy-agent/deploy-agent.compose.yml`
            - `deploy/deploy-agent/deploy-agent.env.example`

            ## Next Steps

            1. Copy the whole `deploy/` directory into your project repository.
            2. Adjust `deploy/release.config.json` to match your Dockerfile, image names, and component list.
            3. Run locally:
               ```bash
               bash deploy/scripts/package_release.sh
               ```
            4. Verify these files are generated:
               - `dist/releases/<version>/manifest.json`
               - `dist/releases/<version>/*.tar`
               - `dist/releases/<version>/sha256sum.txt`
               External images are packaged as tar files too. The script uses `docker build` for build-enabled components and `docker pull` plus `docker save` for external-image components.
            5. In DeployBox, bind the build template to `manifest_script_v1`.
            6. Maintain the deployable services in the component list.
            7. Deploy a compatible deploy-agent on the target host and configure:
               - webhook URL: {webhook_url}
               - status URL: {status_url}
               - shared secret: keep it consistent with `DEPLOY_SHARED_SECRET`
               - if artifacts are stored in a private OSS bucket, also configure:
                 `DEPLOY_OSS_ACCESS_KEY_ID`, `DEPLOY_OSS_ACCESS_KEY_SECRET`,
                 `DEPLOY_OSS_BUCKET_NAME`, `DEPLOY_OSS_ENDPOINT`, `DEPLOY_OSS_REGION`

            ## deploy-agent Quick Start

            1. Prepare a directory on the target host:
               ```bash
               mkdir -p /srv/deploy-agent
               cd /srv/deploy-agent
               ```
            2. Copy these files from the starter package:
               - `deploy/deploy-agent/Dockerfile`
               - `deploy/deploy-agent/requirements.txt`
               - `deploy/deploy-agent/app.py`
               - `deploy/deploy-agent/bin/deploy-release.sh`
               - `deploy/deploy-agent/deploy-agent.compose.yml`
               - `deploy/deploy-agent/deploy-agent.env.example`
            3. Copy the env file and edit it:
               ```bash
               cp deploy-agent.env.example deploy-agent.env
               ```
            4. Start deploy-agent:
               ```bash
               docker compose -f deploy-agent.compose.yml up -d --build
               ```
            5. Check the status endpoint:
               ```bash
               curl http://127.0.0.1:9000/deploy/status
               ```
            6. After the endpoint is reachable, fill webhook URL, status URL, and shared secret back into DeployBox.

            ## Private OSS Buckets

            If release artifacts are stored in a private OSS bucket, the deploy-agent must download them with OSS credentials instead of relying on a public URL.

            Configure these variables in `deploy-agent.env`:

            - `DEPLOY_OSS_ACCESS_KEY_ID`
            - `DEPLOY_OSS_ACCESS_KEY_SECRET`
            - `DEPLOY_OSS_BUCKET_NAME`
            - `DEPLOY_OSS_ENDPOINT`
            - `DEPLOY_OSS_REGION`

            The current deploy protocol will send the manifest payload directly in the webhook request. New OSS releases include `artifact_storage` and `image_tar_object_key`, and the deploy-agent will use OSS SDK download first, then fall back to `image_tar_url` for older releases.

            ## Minimal Hook Payload

            ```json
            {{
              "version": "20260327-1200-abcd123",
              "manifest_url": "https://example.com/releases/20260327-1200-abcd123/manifest.json",
              "triggered_by": "deploybox",
              "commit": "abcd123",
              "environment": "{environment.default_environment_name if environment else 'prod'}"
            }}
            ```
            """
        ).replace("__COMPONENT_LINES__", component_lines)
    component_lines = "\n".join(
        f"- `{component.name}` -> service=`{component.service_name}` image=`{component.image}` mode=`{'docker_build' if component.build_enabled else 'external_image'}`"
        for component in components
        if component.enabled
    ) or "- 还没有启用中的组件，请先在控制台里维护组件"
    return dedent(
        f"""\
        # {project.name} Starter Kit

        这是一份给 Docker / Docker Compose 项目接入 DeployBox 的起步包，目标是把“项目里有哪些可发布组件”先定义清楚，再完成“生成 release -> 发布到远端 deploy-agent”。

        ## 当前组件模型

        __COMPONENT_LINES__
        - 默认工作目录提示：{workspace_hint}
        - 当前平台默认脚本路径提示：{package_script_hint}

        ## 当前包里哪些内容来自项目配置

        以下内容是根据你在 DeployBox 中当前项目的配置实时生成的：

        - 项目名：{project.name}
        - 项目标识：{project.slug}
        - 工作区路径提示：{workspace_hint}
        - 组件列表、service 名、镜像名、Dockerfile、context、是否需要构建
        - 目标环境对应的 webhook URL：{webhook_url}
        - 目标环境对应的 status URL：{status_url}
        - 目标环境对应的逻辑环境名：{environment.default_environment_name if environment else 'prod'}

        如果上面这些信息和你的项目实际情况不一致，应该先回到 DeployBox 修改项目配置，再重新下载 starter。

        ## 当前包里哪些内容是平台默认值

        以下内容是 starter 的通用默认骨架，下载后可以按需调整：

        - 版本策略：`date-shortsha`
        - 制品输出目录：`dist/releases/<version>`
        - deploy-agent 默认监听端口：`9000`
        - deploy-agent 最小实现代码与 compose 示例
        - README 中的示例版本号、示例 commit、演示 URL

        这些默认值不是为某个特定项目硬编码的，它们只是帮助你从 0 接入时先跑通一条主路径。

        当前默认打包规则是：
        - `需要构建` 的组件会执行 `docker build` 后再 `docker save`
        - `外部镜像` 组件会执行 `docker pull` 后再 `docker save`
        - 所以只要组件被选中参与本次 release，默认都会随 release 一起生成 tar 制品

        ## starter 目录结构

        - `README.md`
        - `deploy/release.config.json`
        - `deploy/scripts/package_release.sh`
        - `deploy/deploy-agent/Dockerfile`
        - `deploy/deploy-agent/requirements.txt`
        - `deploy/deploy-agent/app.py`
        - `deploy/deploy-agent/bin/deploy-release.sh`
        - `deploy/deploy-agent/deploy-agent.compose.yml`
        - `deploy/deploy-agent/deploy-agent.env.example`

        ## 你要做的事情

        1. 把 `deploy/` 整个目录放到你的项目仓库。
        2. 按你的 Dockerfile、镜像名和组件数量调整 `deploy/release.config.json`。
        3. 本地先执行：
           ```bash
           bash deploy/scripts/package_release.sh
           ```
        4. 确认生成：
           - `dist/releases/<version>/manifest.json`
           - `dist/releases/<version>/*.tar`
           - `dist/releases/<version>/sha256sum.txt`
        5. 在 DeployBox 的“接入配置”里，把构建模板绑定到 `manifest_script_v1`，并保存覆盖配置。
        6. 在项目组件配置里维护哪些 service 可发布，并设置默认勾选项。
        7. 发版时只勾选本次要更新的组件，例如通常只勾选 `api`。
        8. 远端目标机部署兼容的 deploy-agent，并在环境里填写：
           - webhook URL：{webhook_url}
           - status URL：{status_url}
           - 共享密钥：与远端 `DEPLOY_SHARED_SECRET` 保持一致
           - 如果制品存放在私有 OSS bucket，还需要填写：
             `DEPLOY_OSS_ACCESS_KEY_ID`、`DEPLOY_OSS_ACCESS_KEY_SECRET`、
             `DEPLOY_OSS_BUCKET_NAME`、`DEPLOY_OSS_ENDPOINT`、`DEPLOY_OSS_REGION`

        ## deploy-agent 启动方法

        下面是最小启动流程，适合先把远端接起来：

        1. 在目标机器准备目录，例如：
           ```bash
           mkdir -p /srv/deploy-agent
           cd /srv/deploy-agent
           ```
        2. 把 starter 里的以下文件拷到目标机：
           - `deploy/deploy-agent/Dockerfile`
           - `deploy/deploy-agent/requirements.txt`
           - `deploy/deploy-agent/app.py`
           - `deploy/deploy-agent/bin/deploy-release.sh`
           - `deploy/deploy-agent/deploy-agent.compose.yml`
           - `deploy/deploy-agent/deploy-agent.env.example`
        3. 复制环境变量文件并按实际值修改：
           ```bash
           cp deploy-agent.env.example deploy-agent.env
           ```
        4. 启动 deploy-agent：
           ```bash
           docker compose -f deploy-agent.compose.yml up -d --build
           ```
        5. 检查容器状态：
           ```bash
           docker compose -f deploy-agent.compose.yml ps
           ```
        6. 检查状态接口：
           ```bash
           curl http://127.0.0.1:9000/deploy/status
           ```
        7. 确认接口可达后，把 webhook URL、status URL、shared secret 填回 DeployBox 环境配置。

        ## 私有 OSS Bucket

        如果 release 制品存放在私有 OSS bucket，deploy-agent 不能再依赖公开下载链接，而是需要用本机配置的 OSS 凭证直接下载对象。

        需要在 `deploy-agent.env` 里配置：

        - `DEPLOY_OSS_ACCESS_KEY_ID`
        - `DEPLOY_OSS_ACCESS_KEY_SECRET`
        - `DEPLOY_OSS_BUCKET_NAME`
        - `DEPLOY_OSS_ENDPOINT`
        - `DEPLOY_OSS_REGION`

        当前协议下，DeployBox 会把 manifest 内容直接放进 webhook 请求里。新生成的 OSS release 会额外带上 `artifact_storage` 和 `image_tar_object_key`，deploy-agent 会优先按对象 key 走 OSS SDK 下载；只有旧 release 才继续回退到 `image_tar_url`。

        说明：

        - starter 已经附带 deploy-agent 的最小实现代码，会在目标机本地构建镜像
        - 如果你们后续希望统一维护，也可以把这个镜像先推到公司镜像仓库，再让各目标机直接拉取
        - `DEPLOY_PROJECT_WORKSPACE_HOST_PATH` 必须填写“目标机宿主机上的真实项目目录”，不是容器内 `/workspace`
        - deploy-agent 会同时用这个路径做目录挂载和 `docker compose --project-directory`
        - starter 默认会把这个真实目录同时挂载到容器内同名绝对路径，避免 `env_file` 或宿主机绝对路径在容器里解析失败
        - deploy-agent 会按组件 `service` 自动注入 `DEPLOY_IMAGE_<SERVICE>` 环境变量，推荐你的业务 compose 用这个变量引用发布镜像
        - 如果目标机是 Docker Desktop（macOS/Windows），请先把这个真实目录加入 File Sharing
        - 多项目场景下，不建议把所有项目都写死到同一个 compose；更合理的是每个项目/环境维护一份独立 deploy-agent 配置

        ## 关于目标项目目录挂载

        `deploy-agent.compose.yml` 里的这条配置：

        ```yaml
        - ${{DEPLOY_PROJECT_WORKSPACE_HOST_PATH}}:/workspace
        ```

        作用是把目标机上的业务项目目录挂进 deploy-agent 容器，方便部署脚本在容器内：

        - 读取 `/workspace/docker-compose.yml`
        - 同时在容器内保留宿主机真实路径，兼容 `env_file` 和绝对路径引用
        - 加载 release 中的镜像 tar
        - 按 manifest 中的 `service` 定位要更新的服务
        - 自动注入 `DEPLOY_IMAGE_<SERVICE>` 环境变量，例如 `backend` 会得到 `DEPLOY_IMAGE_BACKEND`
        - 通过 `docker compose --project-directory <宿主机真实目录> up -d ...` 让 compose 用宿主机路径解析 bind mount 和相对路径

        如果一台机器上有多个项目，推荐做法是：

        - 每个项目维护一份独立的 deploy-agent 目录和 `deploy-agent.env`
        - 每份 env 的 `DEPLOY_PROJECT_WORKSPACE_HOST_PATH` 指向各自项目
        - DeployBox 中每个环境对应一个具体项目的 deploy-agent 地址

        不建议把多个项目共用同一个 deploy-agent 工作目录，否则路径、状态和回滚语义都容易混在一起。

        ## 远端 deploy-agent 最小协议

        你的远端服务至少需要暴露：

        - `POST /deploy/hook`
        - `GET /deploy/status`

        `POST /deploy/hook` 最小请求体：

        ```json
        {{
          "version": "20260327-1200-abcd123",
          "manifest_url": "https://example.com/releases/20260327-1200-abcd123/manifest.json",
          "triggered_by": "deploybox",
          "commit": "abcd123",
          "environment": "{environment.default_environment_name if environment else 'prod'}"
        }}
        ```

        ## 轻量接入建议

        - 小团队/单机部署：优先用 Docker Compose + deploy-agent
        - 测试环境：先用本地制品站
        - 正式环境：优先 OSS/CDN
        - 如果你已有 CI：也可以只复用本平台的“上传已有制品”与“发版”部分
        - Compose 文件更适合做“组件候选发现”，最终以控制台中确认后的组件配置为准
        """
    ).replace("__COMPONENT_LINES__", component_lines)


def _starter_remote_agent_steps(lang: str) -> list[str]:
    if lang == "en":
        return [
            "Prepare a deploy-agent directory on the remote Docker host and place the compose and env files there.",
            "Start the deploy-agent service and confirm port 9000 is reachable.",
            "Use a browser or curl to access /deploy/status and confirm it returns JSON.",
            "Fill webhook_url, status_url, and shared_secret back into the environment settings in DeployBox.",
        ]
    return [
        "在远端 Docker 主机准备 deploy-agent 目录，保存 compose 文件和 env 文件。",
        "把 deploy-agent 服务启动起来，并确认 9000 端口可访问。",
        "用浏览器或 curl 访问 /deploy/status，确认返回 JSON。",
        "把 webhook_url、status_url、shared_secret 填回本平台的环境配置。",
    ]


def _render_deploy_agent_compose(project_slug: str) -> str:
    return dedent(
        f"""\
        services:
          deploy-agent:
            build:
              context: .
              dockerfile: ./Dockerfile
            image: local/deploy-agent:{project_slug}
            restart: unless-stopped
            env_file:
              - ./deploy-agent.env
            ports:
              - "${{DEPLOY_PORT:-9000}}:9000"
            volumes:
              - /var/run/docker.sock:/var/run/docker.sock
              - ./state:/deploy/state
              # Mount the project to a stable in-container path for deploy scripts.
              - ${{DEPLOY_PROJECT_WORKSPACE_HOST_PATH:-/srv/apps/{project_slug}}}:/workspace
              # Mirror the same host path inside the container so docker compose
              # can resolve --project-directory, env_file, and bind mounts against
              # the real host path instead of the in-container /workspace path.
              - ${{DEPLOY_PROJECT_WORKSPACE_HOST_PATH:-/srv/apps/{project_slug}}}:${{DEPLOY_PROJECT_WORKSPACE_HOST_PATH:-/srv/apps/{project_slug}}}
        """
    )


def _render_deploy_agent_dockerfile() -> str:
    return dedent(
        """\
        FROM python:3.13-slim

        ARG DEBIAN_MIRROR=mirrors.tuna.tsinghua.edu.cn
        ARG DEBIAN_SECURITY_MIRROR=mirrors.tuna.tsinghua.edu.cn
        ARG DOCKER_APT_MIRROR=mirrors.aliyun.com/docker-ce

        WORKDIR /opt/deploy

        RUN . /etc/os-release \
            && codename="${VERSION_CODENAME:-bookworm}" \
            && cat > /etc/apt/sources.list.d/debian.sources <<EOF
        Types: deb
        URIs: https://${DEBIAN_MIRROR}/debian
        Suites: ${codename} ${codename}-updates
        Components: main
        Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

        Types: deb
        URIs: https://${DEBIAN_SECURITY_MIRROR}/debian-security
        Suites: ${codename}-security
        Components: main
        Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
        EOF
        RUN apt-get update \
            && apt-get install -y --no-install-recommends bash curl ca-certificates gnupg \
            && install -m 0755 -d /etc/apt/keyrings \
            && curl --http1.1 --retry 5 --retry-all-errors --retry-delay 2 -fsSL https://${DOCKER_APT_MIRROR}/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
            && chmod a+r /etc/apt/keyrings/docker.asc \
            && . /etc/os-release \
            && docker_repo_codename="${VERSION_CODENAME}" \
            && case "${docker_repo_codename}" in trixie|forky|sid|unstable) docker_repo_codename="bookworm" ;; esac \
            && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://${DOCKER_APT_MIRROR}/linux/debian ${docker_repo_codename} stable" > /etc/apt/sources.list.d/docker.list \
            && apt-get update \
            && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
            && if [ -x /usr/libexec/docker/cli-plugins/docker-compose ]; then ln -sf /usr/libexec/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose; fi \
            && if [ -x /usr/lib/docker/cli-plugins/docker-compose ]; then ln -sf /usr/lib/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose; fi \
            && rm -rf /var/lib/apt/lists/*

        COPY requirements.txt /opt/deploy/requirements.txt
        RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple \
            && pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn
        RUN pip install --no-cache-dir -r /opt/deploy/requirements.txt

        COPY app.py /opt/deploy/app.py
        COPY bin/deploy-release.sh /opt/deploy/bin/deploy-release.sh
        RUN chmod +x /opt/deploy/bin/deploy-release.sh

        CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
        """
    )


def _render_deploy_agent_requirements() -> str:
    return dedent(
        """\
        fastapi==0.116.1
        uvicorn==0.35.0
        httpx==0.28.1
        oss2==2.19.1
        """
    )


def _render_deploy_agent_app(lang: str = "zh") -> str:
    manifest_detail = "Loading manifest" if lang == "en" else "正在加载 manifest"
    failed_detail = "Deploy script failed" if lang == "en" else "部署脚本执行失败"
    success_detail = "Deployment completed" if lang == "en" else "部署完成"
    return dedent(
        """\
        import hashlib
        import hmac
        import json
        import os
        import subprocess
        from datetime import datetime, timezone
        from pathlib import Path

        import httpx
        from fastapi import FastAPI, HTTPException, Request


        app = FastAPI(title="deploy-agent")
        STATE_DIR = Path(os.environ.get("DEPLOY_STATE_DIR", "/deploy/state")).resolve()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATUS_FILE = STATE_DIR / "status.json"


        def utcnow() -> str:
            return datetime.now(timezone.utc).isoformat()


        def load_status() -> dict:
            if STATUS_FILE.exists():
                return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            return {{
                "status": "idle",
                "version": "",
                "updated_at": utcnow(),
            }}


        def save_status(payload: dict) -> None:
            payload["updated_at"] = utcnow()
            STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


        def verify_signature(raw_body: bytes, signature_header: str | None) -> None:
            secret = os.environ.get("DEPLOY_SHARED_SECRET", "").encode("utf-8")
            if not secret:
                raise HTTPException(status_code=500, detail="DEPLOY_SHARED_SECRET_missing")
            if not signature_header or not signature_header.startswith("sha256="):
                raise HTTPException(status_code=401, detail="signature_missing")
            expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
            received = signature_header.split("=", 1)[1]
            if not hmac.compare_digest(expected, received):
                raise HTTPException(status_code=401, detail="signature_invalid")


        @app.get("/deploy/status")
        def status():
            return load_status()


        @app.post("/deploy/hook")
        async def deploy_hook(request: Request):
            raw_body = await request.body()
            verify_signature(raw_body, request.headers.get("X-Signature"))
            payload = json.loads(raw_body.decode("utf-8"))
            version = str(payload.get("version") or "").strip()
            manifest_url = str(payload.get("manifest_url") or "").strip()
            manifest_payload = payload.get("manifest_json")
            if not version:
                raise HTTPException(status_code=400, detail="version_missing")
            if manifest_payload is not None and not isinstance(manifest_payload, dict):
                raise HTTPException(status_code=400, detail="manifest_json_invalid")
            if manifest_payload is None and not manifest_url:
                raise HTTPException(status_code=400, detail="version_or_manifest_missing")

            release_dir = STATE_DIR / "releases" / version
            release_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = release_dir / "manifest.json"

            save_status(
                {{
                    "status": "running",
                    "version": version,
                    "manifest_url": manifest_url,
                    "detail": "{manifest_detail}",
                }}
            )

            if manifest_payload is not None:
                manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                async with httpx.AsyncClient(timeout=60) as client:
                    response = await client.get(manifest_url)
                    response.raise_for_status()
                    manifest_path.write_bytes(response.content)

            env = os.environ.copy()
            env["DEPLOY_VERSION"] = version
            env["DEPLOY_MANIFEST_URL"] = manifest_url
            env["DEPLOY_MANIFEST_PATH"] = str(manifest_path)
            env["DEPLOY_TRIGGERED_BY"] = str(payload.get("triggered_by") or "deploybox")
            env["DEPLOY_COMMIT"] = str(payload.get("commit") or "")
            env["DEPLOY_ENVIRONMENT"] = str(payload.get("environment") or env.get("DEPLOY_ENVIRONMENT", "prod"))

            process = subprocess.run(
                [env.get("DEPLOY_SCRIPT", "/opt/deploy/bin/deploy-release.sh")],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            stdout = (process.stdout or "").strip()
            stderr = (process.stderr or "").strip()
            if process.returncode != 0:
                save_status(
                    {{
                        "status": "failed",
                        "version": version,
                        "manifest_url": manifest_url,
                        "stdout": stdout[-4000:],
                        "stderr": stderr[-4000:],
                        "detail": "{failed_detail}",
                    }}
                )
                return {{
                    "status": "failed",
                    "version": version,
                    "stdout": stdout[-4000:],
                    "stderr": stderr[-4000:],
                }}

            save_status(
                {{
                    "status": "success",
                    "version": version,
                    "manifest_url": manifest_url,
                    "stdout": stdout[-4000:],
                    "stderr": stderr[-4000:],
                    "detail": "{success_detail}",
                }}
            )
            return {{
                "status": "success",
                "version": version,
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
            }}
        """
    ).format(
        manifest_detail=manifest_detail,
        failed_detail=failed_detail,
        success_detail=success_detail,
    )


def _render_deploy_release_script() -> str:
    return dedent(
        """\
        #!/usr/bin/env bash
        set -euo pipefail

        MANIFEST_PATH="${DEPLOY_MANIFEST_PATH:?DEPLOY_MANIFEST_PATH is required}"
        PROJECT_ROOT="${DEPLOY_PROJECT_ROOT:-/workspace}"

        if [[ ! -f "${MANIFEST_PATH}" ]]; then
          echo "manifest not found: ${MANIFEST_PATH}" >&2
          exit 1
        fi

        python3 - <<'PY'
        import json
        import os
        import shutil
        import subprocess
        from pathlib import Path
        from urllib.request import urlretrieve

        import oss2


        def compose_base_command() -> list[str]:
            if shutil.which("docker"):
                probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, check=False)
                if probe.returncode == 0:
                    return ["docker", "compose"]
            if shutil.which("docker-compose"):
                probe = subprocess.run(["docker-compose", "version"], capture_output=True, text=True, check=False)
                if probe.returncode == 0:
                    return ["docker-compose"]
            raise RuntimeError("docker compose is not available inside deploy-agent")


        def normalize_endpoint(value: str) -> str:
            raw = str(value or "").strip()
            if not raw:
                return ""
            if raw.startswith("http://") or raw.startswith("https://"):
                return raw.rstrip("/")
            return f"https://{raw.rstrip('/')}"


        def build_oss_bucket(storage_info: dict):
            provider = str(storage_info.get("provider") or os.environ.get("DEPLOY_ARTIFACT_PROVIDER") or "").strip()
            if provider and provider != "aliyun_oss":
                raise RuntimeError(f"unsupported artifact storage provider: {provider}")
            access_key_id = str(os.environ.get("DEPLOY_OSS_ACCESS_KEY_ID") or "").strip()
            access_key_secret = str(os.environ.get("DEPLOY_OSS_ACCESS_KEY_SECRET") or "").strip()
            bucket_name = str(storage_info.get("bucket") or os.environ.get("DEPLOY_OSS_BUCKET_NAME") or "").strip()
            endpoint = normalize_endpoint(storage_info.get("endpoint") or os.environ.get("DEPLOY_OSS_ENDPOINT") or "")
            region = str(storage_info.get("region") or os.environ.get("DEPLOY_OSS_REGION") or "").strip() or None
            required = {
                "DEPLOY_OSS_ACCESS_KEY_ID": access_key_id,
                "DEPLOY_OSS_ACCESS_KEY_SECRET": access_key_secret,
                "DEPLOY_OSS_BUCKET_NAME": bucket_name,
                "DEPLOY_OSS_ENDPOINT": endpoint,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise RuntimeError(
                    "manifest requires private OSS download, missing deploy-agent config: " + ", ".join(missing)
                )
            auth = oss2.Auth(access_key_id, access_key_secret)
            return oss2.Bucket(auth, endpoint, bucket_name, region=region)


        manifest_path = Path(os.environ["DEPLOY_MANIFEST_PATH"])
        project_root = Path(os.environ.get("DEPLOY_PROJECT_ROOT", "/workspace"))
        project_host_root = str(
            os.environ.get("DEPLOY_PROJECT_WORKSPACE_REAL_PATH")
            or os.environ.get("DEPLOY_PROJECT_WORKSPACE_HOST_PATH")
            or project_root
        ).strip()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = str(payload.get("version") or "")
        services = []
        compose_env = os.environ.copy()
        compose_cmd = compose_base_command()
        artifact_storage = payload.get("artifact_storage") if isinstance(payload.get("artifact_storage"), dict) else {}
        oss_bucket = None

        for component in payload.get("components", []):
            image = str(component.get("image") or "").strip()
            tar_url = str(component.get("image_tar_url") or "").strip()
            object_key = str(component.get("image_tar_object_key") or "").strip()
            service = str(component.get("service") or component.get("name") or "").strip()
            if service:
                services.append(service)
            if service and image:
                env_key = "DEPLOY_IMAGE_" + "".join(ch if ch.isalnum() else "_" for ch in service).upper()
                compose_env[env_key] = image
                print(f"compose image override: {env_key}={image}")
            if object_key:
                tar_name = Path(object_key).name
                local_tar = manifest_path.parent / tar_name
                if oss_bucket is None:
                    oss_bucket = build_oss_bucket(artifact_storage)
                print(f"downloading private oss object: {object_key}")
                oss_bucket.get_object_to_file(object_key, str(local_tar))
                subprocess.run(["docker", "load", "-i", str(local_tar)], check=True)
            elif tar_url:
                tar_name = Path(tar_url).name
                local_tar = manifest_path.parent / tar_name
                print(f"downloading {tar_url}")
                urlretrieve(tar_url, local_tar)
                subprocess.run(["docker", "load", "-i", str(local_tar)], check=True)
            elif image:
                print(f"pulling external image: {image}")
                subprocess.run(["docker", "pull", image], check=True)

        compose_path = project_root / "docker-compose.yml"
        if compose_path.exists():
            print(f"restarting compose stack: {compose_path}")
            print(f"compose project directory: {project_host_root}")
            command = [
                *compose_cmd,
                "-f",
                str(compose_path),
                "--project-directory",
                project_host_root,
                "up",
                "-d",
                "--no-deps",
                "--no-build",
            ]
            if services:
                deduped = []
                seen = set()
                for service in services:
                    if service not in seen:
                        seen.add(service)
                        deduped.append(service)
                command.extend(deduped)
            subprocess.run(command, cwd=project_root, env=compose_env, check=True)
        else:
            print("docker-compose.yml not found under project root, skip compose up")

        print(f"deployment finished for version {version}")
        PY
        """
    )


def _render_deploy_agent_env(
    *,
    webhook_url: str,
    status_url: str,
    shared_secret: str,
    deploy_environment: str,
    remote_prefix: str,
    lang: str = "zh",
) -> str:
    if lang == "en":
        return dedent(
            f"""\
            # Copy this file to deploy-agent.env and update it for your environment
            DEPLOY_SHARED_SECRET={shared_secret}
            DEPLOY_PORT=9000
            DEPLOY_BIND=0.0.0.0
            DEPLOY_STATE_DIR=/deploy/state
            DEPLOY_SCRIPT=/opt/deploy/bin/deploy-release.sh
            DEPLOY_ENVIRONMENT={deploy_environment}
            DEPLOY_PROJECT_ROOT=/workspace
            DEPLOY_OSS_ACCESS_KEY_ID=
            DEPLOY_OSS_ACCESS_KEY_SECRET=
            DEPLOY_OSS_BUCKET_NAME=
            DEPLOY_OSS_ENDPOINT=
            DEPLOY_OSS_REGION=

            # This must be the real project path on the target host.
            # It is used for mounting the project into /workspace.
            # It is also used as docker compose --project-directory.
            # Keep bind mounts and relative paths resolved from the host path.
            #
            # macOS example:
            # /Users/yourname/workspace/sample-service
            #
            # Linux example:
            # /srv/apps/{remote_prefix.split('/')[0]}
            #
            # On Docker Desktop, add this path to File Sharing first.
            DEPLOY_PROJECT_WORKSPACE_HOST_PATH=/srv/apps/{remote_prefix.split('/')[0]}

            # Reference values for DeployBox onboarding. These are not read directly by deploy-agent.
            # webhook_url={webhook_url}
            # status_url={status_url}
            # artifact_prefix={remote_prefix}
            """
        )
    return dedent(
        f"""\
        # 复制为 deploy-agent.env 后按实际情况修改
        DEPLOY_SHARED_SECRET={shared_secret}
        DEPLOY_PORT=9000
        DEPLOY_BIND=0.0.0.0
        DEPLOY_STATE_DIR=/deploy/state
        DEPLOY_SCRIPT=/opt/deploy/bin/deploy-release.sh
        DEPLOY_ENVIRONMENT={deploy_environment}
        DEPLOY_PROJECT_ROOT=/workspace
        DEPLOY_OSS_ACCESS_KEY_ID=
        DEPLOY_OSS_ACCESS_KEY_SECRET=
        DEPLOY_OSS_BUCKET_NAME=
        DEPLOY_OSS_ENDPOINT=
        DEPLOY_OSS_REGION=

        # 必须填写目标机宿主机上的真实项目目录。
        # 这个值会用于挂载到容器内的 /workspace。
        # 这个值也会作为 docker compose 的 --project-directory。
        # 这样 compose 里的 bind mount / 相对路径才能按宿主机目录解析。
        #
        # macOS 例子：
        # /Users/yourname/workspace/sample-service
        #
        # Linux 例子：
        # /srv/apps/{remote_prefix.split('/')[0]}
        #
        # Docker Desktop 还需要把这个目录加入 File Sharing。
        DEPLOY_PROJECT_WORKSPACE_HOST_PATH=/srv/apps/{remote_prefix.split('/')[0]}

        # 下方是给接入平台时参考的，不会直接被 deploy-agent 读取
        # webhook_url={webhook_url}
        # status_url={status_url}
        # artifact_prefix={remote_prefix}
        """
    )
