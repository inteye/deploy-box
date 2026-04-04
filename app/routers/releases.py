from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..auth import get_current_user, require_permission
from ..database import get_db
from ..models import Project, Release
from ..schemas import ReleaseCreate, ReleaseRead
from ..services import sync_release_manifest


router = APIRouter(prefix="/api/releases", tags=["releases"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[ReleaseRead])
def list_releases(project_id: int | None = Query(default=None), db: Session = Depends(get_db)):
    stmt = select(Release).options(joinedload(Release.components)).order_by(Release.created_at.desc())
    if project_id is not None:
        stmt = stmt.where(Release.project_id == project_id)
    return db.scalars(stmt).unique().all()


@router.get("/{release_id}", response_model=ReleaseRead)
def get_release(release_id: int, db: Session = Depends(get_db)):
    release = db.execute(
        select(Release).options(joinedload(Release.components)).where(Release.id == release_id)
    ).unique().scalar_one_or_none()
    if not release:
        raise HTTPException(status_code=404, detail="release_not_found")
    return release


@router.post("", response_model=ReleaseRead)
def create_release(
    payload: ReleaseCreate,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("release.manage")),
):
    project = db.get(Project, payload.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project_not_found")
    if db.scalar(
        select(Release).where(Release.project_id == payload.project_id, Release.version == payload.version)
    ):
        raise HTTPException(status_code=409, detail="release_version_exists")
    release = Release(**payload.model_dump())
    db.add(release)
    db.commit()
    db.refresh(release)
    return sync_release_manifest(db, release)


@router.post("/{release_id}/sync-manifest", response_model=ReleaseRead)
def sync_manifest(
    release_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("release.manage")),
):
    release = db.execute(
        select(Release).options(joinedload(Release.components)).where(Release.id == release_id)
    ).unique().scalar_one_or_none()
    if not release:
        raise HTTPException(status_code=404, detail="release_not_found")
    return sync_release_manifest(db, release)
