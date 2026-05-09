from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..i18n import get_locale, get_translation, translate_runtime_text
from ..models import BuildJob, BuildJobEvent
from ..schemas import BuildJobEventRead, BuildJobRead


router = APIRouter(prefix="/api/build-jobs", tags=["build-jobs"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[BuildJobRead])
def list_build_jobs(db: Session = Depends(get_db)):
    return db.scalars(select(BuildJob).order_by(BuildJob.created_at.desc()).limit(100)).all()


@router.get("/{build_job_id}")
def get_build_job(build_job_id: int, request: Request, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    build_job = db.get(BuildJob, build_job_id)
    if not build_job:
        raise HTTPException(status_code=404, detail="build_job_not_found")
    translation = get_translation(get_locale(request, current_user))
    return {
        "id": build_job.id,
        "project_id": build_job.project_id,
        "environment_id": build_job.environment_id,
        "template_id": build_job.template_id,
        "status": build_job.status,
        "current_stage": build_job.current_stage,
        "progress_percent": build_job.progress_percent,
        "output_version": build_job.output_version,
        "storage_mode": build_job.storage_mode,
        "artifact_mode": build_job.artifact_mode,
        "manifest_url": build_job.manifest_url,
        "result_json": build_job.result_json,
        "log_excerpt": translate_runtime_text(translation, build_job.log_excerpt),
        "triggered_by": build_job.triggered_by,
        "started_at": build_job.started_at,
        "finished_at": build_job.finished_at,
        "created_at": build_job.created_at,
        "updated_at": build_job.updated_at,
    }


@router.get("/{build_job_id}/events", response_model=list[BuildJobEventRead])
def get_build_job_events(
    build_job_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    build_job = db.get(BuildJob, build_job_id)
    if not build_job:
        raise HTTPException(status_code=404, detail="build_job_not_found")
    translation = get_translation(get_locale(request, current_user))
    events = db.scalars(
        select(BuildJobEvent).where(BuildJobEvent.build_job_id == build_job_id).order_by(BuildJobEvent.created_at.asc())
    ).all()
    return [
        {
            "id": event.id,
            "build_job_id": event.build_job_id,
            "event_type": event.event_type,
            "stage": event.stage,
            "message": translate_runtime_text(translation, event.message) or "",
            "progress_percent": event.progress_percent,
            "created_at": event.created_at,
        }
        for event in events
    ]
