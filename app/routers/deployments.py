from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user, require_permission
from ..database import get_db
from ..i18n import get_locale, get_translation, translate_runtime_text
from ..models import Deployment, Environment, OperatorUser, Project, Release
from ..schemas import DeploymentCreate, DeploymentRead
from ..services import refresh_deployment_status as refresh_deployment_status_service, run_deployment


router = APIRouter(prefix="/api/deployments", tags=["deployments"], dependencies=[Depends(get_current_user)])


def _translate_deployment(translation, deployment):
    return {
        "id": deployment.id,
        "project_id": deployment.project_id,
        "environment_id": deployment.environment_id,
        "release_id": deployment.release_id,
        "status": deployment.status,
        "status_reason": translate_runtime_text(translation, deployment.status_reason),
        "progress_percent": deployment.progress_percent,
        "triggered_by": deployment.triggered_by,
        "submitted_at": deployment.submitted_at,
        "finished_at": deployment.finished_at,
        "last_polled_at": deployment.last_polled_at,
        "log_excerpt": translate_runtime_text(translation, deployment.log_excerpt),
        "adapter_response_json": deployment.adapter_response_json,
        "last_status_json": deployment.last_status_json,
        "created_at": deployment.created_at,
        "updated_at": deployment.updated_at,
    }


@router.get("", response_model=list[DeploymentRead])
def list_deployments(db: Session = Depends(get_db)):
    return db.scalars(select(Deployment).order_by(Deployment.created_at.desc())).all()


@router.get("/{deployment_id}")
def get_deployment(deployment_id: int, request: Request, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    deployment = db.get(Deployment, deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="deployment_not_found")
    translation = get_translation(get_locale(request, current_user))
    return _translate_deployment(translation, deployment)


@router.post("", response_model=DeploymentRead)
def create_deployment(
    payload: DeploymentCreate,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("release.manage")),
):
    project = db.get(Project, payload.project_id)
    environment = db.get(Environment, payload.environment_id)
    release = db.get(Release, payload.release_id)
    if not project or not environment or not release:
        raise HTTPException(status_code=404, detail="project_environment_or_release_not_found")
    if environment.project_id != project.id or release.project_id != project.id:
        raise HTTPException(status_code=400, detail="project_binding_mismatch")
    return run_deployment(
        db,
        project=project,
        environment=environment,
        release=release,
        triggered_by=payload.triggered_by or current_user.username,
    )


@router.post("/{deployment_id}/refresh-status")
def refresh_deployment_status(
    deployment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("release.manage")),
):
    deployment = db.get(Deployment, deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="deployment_not_found")
    try:
        deployment = refresh_deployment_status_service(deployment, db)
    except Exception as exc:  # pragma: no cover - defensive path
        raise HTTPException(status_code=502, detail=f"status_refresh_failed: {exc}") from exc
    translation = get_translation(get_locale(request, current_user))
    return _translate_deployment(translation, deployment)
