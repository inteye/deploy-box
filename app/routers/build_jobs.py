from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import BuildJob, BuildJobEvent
from ..schemas import BuildJobEventRead, BuildJobRead


router = APIRouter(prefix="/api/build-jobs", tags=["build-jobs"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[BuildJobRead])
def list_build_jobs(db: Session = Depends(get_db)):
    return db.scalars(select(BuildJob).order_by(BuildJob.created_at.desc()).limit(100)).all()


@router.get("/{build_job_id}", response_model=BuildJobRead)
def get_build_job(build_job_id: int, db: Session = Depends(get_db)):
    build_job = db.get(BuildJob, build_job_id)
    if not build_job:
        raise HTTPException(status_code=404, detail="build_job_not_found")
    return build_job


@router.get("/{build_job_id}/events", response_model=list[BuildJobEventRead])
def get_build_job_events(build_job_id: int, db: Session = Depends(get_db)):
    build_job = db.get(BuildJob, build_job_id)
    if not build_job:
        raise HTTPException(status_code=404, detail="build_job_not_found")
    return db.scalars(
        select(BuildJobEvent).where(BuildJobEvent.build_job_id == build_job_id).order_by(BuildJobEvent.created_at.asc())
    ).all()
