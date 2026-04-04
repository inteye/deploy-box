import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user, require_permission
from ..database import get_db
from ..models import BuildTemplate, Environment, Project, ProjectBuildConfig
from ..schemas import EnvironmentCreate, EnvironmentRead, EnvironmentUpdate, ProjectCreate, ProjectRead, ProjectUpdate
from ..services import ensure_default_project_components, ensure_project_build_config, normalize_build_config_override, save_project_build_config


router = APIRouter(prefix="/api/projects", tags=["projects"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[ProjectRead])
def list_projects(db: Session = Depends(get_db)):
    return db.scalars(select(Project).order_by(Project.created_at.desc())).all()


@router.post("", response_model=ProjectRead)
def create_project(
    payload: ProjectCreate,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("project.manage")),
):
    if db.scalar(select(Project).where(Project.slug == payload.slug)):
        raise HTTPException(status_code=409, detail="project_slug_exists")
    project = Project(
        name=payload.name,
        slug=payload.slug,
        adapter_type=payload.adapter_type,
        workspace_path=(payload.workspace_path or "").strip() or None,
        image_registry_prefix=(payload.image_registry_prefix or "").strip().rstrip("/") or None,
        default_artifact_repository_id=payload.default_artifact_repository_id,
        description=payload.description,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    ensure_project_build_config(db, project)
    ensure_default_project_components(db, project)
    return project


@router.put("/{project_id}", response_model=ProjectRead)
def update_project(
    project_id: int,
    payload: ProjectUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("project.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project_not_found")
    duplicate = db.scalar(select(Project).where(Project.slug == payload.slug, Project.id != project_id))
    if duplicate:
        raise HTTPException(status_code=409, detail="project_slug_exists")
    project.name = payload.name
    project.slug = payload.slug
    project.workspace_path = (payload.workspace_path or "").strip() or None
    project.image_registry_prefix = (payload.image_registry_prefix or "").strip().rstrip("/") or None
    project.default_artifact_repository_id = payload.default_artifact_repository_id
    project.description = payload.description
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("project.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project_not_found")
    db.delete(project)
    db.commit()


@router.get("/{project_id}/environments", response_model=list[EnvironmentRead])
def list_environments(project_id: int, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project_not_found")
    return db.scalars(
        select(Environment).where(Environment.project_id == project_id).order_by(Environment.created_at.desc())
    ).all()


@router.post("/{project_id}/environments", response_model=EnvironmentRead)
def create_environment(
    project_id: int,
    payload: EnvironmentCreate,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("project.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project_not_found")
    environment = Environment(project_id=project_id, **payload.model_dump())
    db.add(environment)
    db.commit()
    db.refresh(environment)
    return environment


@router.put("/{project_id}/environments/{environment_id}", response_model=EnvironmentRead)
def update_environment(
    project_id: int,
    environment_id: int,
    payload: EnvironmentUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("project.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project_not_found")
    environment = db.get(Environment, environment_id)
    if not environment or environment.project_id != project_id:
        raise HTTPException(status_code=404, detail="environment_not_found")
    for field, value in payload.model_dump().items():
        setattr(environment, field, value)
    db.add(environment)
    db.commit()
    db.refresh(environment)
    return environment


@router.delete("/{project_id}/environments/{environment_id}", status_code=204)
def delete_environment(
    project_id: int,
    environment_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("project.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project_not_found")
    environment = db.get(Environment, environment_id)
    if not environment or environment.project_id != project_id:
        raise HTTPException(status_code=404, detail="environment_not_found")
    if environment.deployments:
        raise HTTPException(status_code=409, detail="environment_has_deployments")
    db.delete(environment)
    db.commit()


@router.get("/{project_id}/build-config")
def get_project_build_config(project_id: int, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project_not_found")
    build_config = db.scalar(select(ProjectBuildConfig).where(ProjectBuildConfig.project_id == project_id))
    if not build_config:
        build_config = ensure_project_build_config(db, project)
    template = db.get(BuildTemplate, build_config.template_id)
    return {
        "project_id": project_id,
        "build_config": {
            "id": build_config.id,
            "template_id": build_config.template_id,
            "config_override_json": build_config.config_override_json,
            "enabled": build_config.enabled,
        },
        "template": {
            "id": template.id,
            "name": template.name,
            "slug": template.slug,
            "strategy": template.strategy,
            "config_json": template.config_json,
        }
        if template
        else None,
    }


@router.put("/{project_id}/build-config")
def update_project_build_config(
    project_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("project.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project_not_found")
    template_id = int(payload.get("template_id") or 0)
    if not template_id:
        raise HTTPException(status_code=400, detail="template_id_required")
    raw_override = payload.get("config_override_json")
    normalized_override = normalize_build_config_override(raw_override) if raw_override else {}
    build_config = save_project_build_config(
        db,
        project=project,
        template_id=template_id,
        config_override_json=json.dumps(normalized_override, ensure_ascii=True) if normalized_override else "",
    )
    return {
        "id": build_config.id,
        "template_id": build_config.template_id,
        "config_override_json": build_config.config_override_json,
        "enabled": build_config.enabled,
    }
