from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Deployment, Environment, OperatorUser, Project, Release
from ..schemas import DeploymentCreate, DeploymentRead
from ..services import refresh_deployment_status, run_deployment


router = APIRouter(prefix="/api/deployments", tags=["deployments"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[DeploymentRead])
def list_deployments(db: Session = Depends(get_db)):
    return db.scalars(select(Deployment).order_by(Deployment.created_at.desc())).all()


@router.get("/{deployment_id}", response_model=DeploymentRead)
def get_deployment(deployment_id: int, db: Session = Depends(get_db)):
    deployment = db.get(Deployment, deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="deployment_not_found")
    return deployment


@router.post("", response_model=DeploymentRead)
def create_deployment(
    payload: DeploymentCreate,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(get_current_user),
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


@router.post("/{deployment_id}/refresh-status", response_model=DeploymentRead)
def refresh_deployment_status(deployment_id: int, db: Session = Depends(get_db)):
    deployment = db.get(Deployment, deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="deployment_not_found")
    try:
        deployment = refresh_deployment_status(deployment, db)
    except Exception as exc:  # pragma: no cover - defensive path
        raise HTTPException(status_code=502, detail=f"status_refresh_failed: {exc}") from exc
    return deployment
