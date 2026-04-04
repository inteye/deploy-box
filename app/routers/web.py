import json
from pathlib import Path
from urllib.parse import quote_plus, urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ..auth import (
    ROLE_DEFINITIONS,
    authenticate_user,
    get_current_user,
    get_optional_user,
    list_accessible_project_ids,
    list_user_role_bindings,
    list_user_role_codes,
    require_any_permission,
    require_permission,
    user_has_permission,
)
from ..config import get_settings
from ..database import get_db
from ..i18n import get_locale, get_translation, translate_runtime_text
from ..models import ArtifactRepository, AuditLog, BuildJob, BuildJobEvent, BuildTemplate, Deployment, Environment, OperatorUser, Project, ProjectBuildConfig, ProjectComponent, Release
from ..services import (
    analyze_compose_release_readiness,
    build_quality_status_summary,
    build_quality_trend_points,
    build_starter_archive,
    build_starter_bundle,
    create_build_job,
    delete_project_component,
    ensure_default_project_components,
    ensure_project_build_config,
    ensure_project_governance,
    artifact_repository_public_summary,
    get_project_artifact_repository,
    get_system_settings_map,
    import_project_components_from_compose,
    list_artifact_repositories,
    list_recent_quality_checks,
    list_users,
    list_project_components,
    normalize_project_component_images,
    normalize_build_config_override,
    parse_release_checklist,
    record_audit_log,
    refresh_deployment_status,
    resolve_project_artifact_mode,
    resolve_project_workspace,
    run_deployment,
    run_due_quality_checks,
    run_project_quality_check,
    save_project_component,
    save_project_build_config,
    save_artifact_repository,
    save_operator_user,
    save_project_governance,
    save_system_settings,
    sync_release_manifest,
    sync_release_manifest_payload,
)
from ..storage import build_artifact_storage, build_oss_storage_descriptor


router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.add_extension("jinja2.ext.i18n")


def render(request: Request, template_name: str, **context):
    user = context.get("current_user")
    lang = get_locale(request, user)
    translation = get_translation(lang)
    templates.env.install_gettext_translations(translation, newstyle=True)  # type: ignore[attr-defined]
    base_context = {
        "request": request,
        "current_path": request.url.path,
        "current_lang": lang,
        "current_user_role_codes": list_user_role_codes(user) if user else [],
        "has_project_manage_permission": user_has_permission(user, "project.manage") if user else False,
        "has_build_manage_permission": user_has_permission(user, "build.manage") if user else False,
        "has_release_manage_permission": user_has_permission(user, "release.manage") if user else False,
        "has_audit_read_permission": user_has_permission(user, "audit.read") if user else False,
        "has_system_manage_permission": user_has_permission(user, "system.manage") if user else False,
    }
    base_context.update(context)
    return templates.TemplateResponse(template_name, base_context)


def tr(request: Request, current_user: OperatorUser | None, message: str) -> str:
    return get_translation(get_locale(request, current_user)).gettext(message)


def try_parse_json(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _parse_optional_int(raw: str | None) -> int | None:
    value = (raw or "").strip()
    if not value:
        return None
    return int(value)


def success_redirect(url: str, message: str) -> RedirectResponse:
    separator = "&" if "?" in url else "?"
    return RedirectResponse(url=f"{url}{separator}notice={quote_plus(message)}", status_code=status.HTTP_303_SEE_OTHER)


def error_redirect(url: str, message: str) -> RedirectResponse:
    separator = "&" if "?" in url else "?"
    return RedirectResponse(url=f"{url}{separator}error={quote_plus(message)}", status_code=status.HTTP_303_SEE_OTHER)


def _parse_page(raw: str | None) -> int:
    if raw and raw.isdigit():
        return max(int(raw), 1)
    return 1


def _build_project_detail_url(request: Request, project_id: int, **updates: int | str | None) -> str:
    params = {key: value for key, value in request.query_params.items()}
    for key, value in updates.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = str(value)
    query = urlencode(params)
    base_url = f"/console/projects/{project_id}"
    return f"{base_url}?{query}" if query else base_url


def _paginate_section(*, total: int, page: int, per_page: int, request: Request, project_id: int, param_name: str) -> dict:
    total_pages = max((total - 1) // per_page + 1, 1)
    current_page = min(max(page, 1), total_pages)
    page_start = max(current_page - 2, 1)
    page_end = min(page_start + 4, total_pages)
    page_start = max(page_end - 4, 1)
    return {
        "page": current_page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "offset": (current_page - 1) * per_page,
        "pages": [
            {
                "number": number,
                "url": _build_project_detail_url(request, project_id, **{param_name: number}),
                "current": number == current_page,
            }
            for number in range(page_start, page_end + 1)
        ],
        "prev_url": _build_project_detail_url(request, project_id, **{param_name: current_page - 1}) if current_page > 1 else None,
        "next_url": _build_project_detail_url(request, project_id, **{param_name: current_page + 1}) if current_page < total_pages else None,
    }


def _build_url(request: Request, base_url: str, **updates: int | str | None) -> str:
    params = {key: value for key, value in request.query_params.items()}
    for key, value in updates.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = str(value)
    query = urlencode(params)
    return f"{base_url}?{query}" if query else base_url


def _paginate_path(*, total: int, page: int, per_page: int, request: Request, base_url: str, param_name: str) -> dict:
    total_pages = max((total - 1) // per_page + 1, 1)
    current_page = min(max(page, 1), total_pages)
    page_start = max(current_page - 2, 1)
    page_end = min(page_start + 4, total_pages)
    page_start = max(page_end - 4, 1)
    return {
        "page": current_page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "offset": (current_page - 1) * per_page,
        "pages": [
            {
                "number": number,
                "url": _build_url(request, base_url, **{param_name: number}),
                "current": number == current_page,
            }
            for number in range(page_start, page_end + 1)
        ],
        "prev_url": _build_url(request, base_url, **{param_name: current_page - 1}) if current_page > 1 else None,
        "next_url": _build_url(request, base_url, **{param_name: current_page + 1}) if current_page < total_pages else None,
    }


@router.get("/console/fs/directories")
def list_workspace_directories(
    path: str = "",
    current_user: OperatorUser = Depends(require_permission("project.manage")),
):
    root = Path(get_settings().workspace_path).resolve()
    requested = path.strip().strip("/")
    current = (root / requested).resolve() if requested else root
    if root not in current.parents and current != root:
        return JSONResponse({"detail": "path_out_of_workspace"}, status_code=400)
    if not current.exists() or not current.is_dir():
        return JSONResponse({"detail": "directory_not_found"}, status_code=404)
    entries = []
    for item in sorted(current.iterdir(), key=lambda entry: entry.name.lower()):
        if item.is_dir():
            relative = item.relative_to(root).as_posix()
            entries.append({"name": item.name, "path": relative})
    current_relative = current.relative_to(root).as_posix() if current != root else ""
    parent_relative = current.parent.relative_to(root).as_posix() if current != root else None
    return {
        "root": str(root),
        "current_path": current_relative,
        "parent_path": parent_relative,
        "entries": entries,
    }


@router.get("/console/projects/{project_id}/workspace/validate")
def validate_project_workspace(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("project.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        return JSONResponse({"detail": "project_not_found"}, status_code=404)
    settings = get_settings()
    configured = (project.workspace_path or "").strip()
    resolved = resolve_project_workspace(project, settings)
    path_exists = resolved.exists()
    is_dir = resolved.is_dir() if path_exists else False
    compose_path = resolved / "docker-compose.yml"
    package_script_path = resolved / settings.package_script
    return {
        "workspace_root": str(Path(settings.workspace_path).resolve()),
        "configured_path": configured,
        "resolved_path": str(resolved),
        "exists": path_exists,
        "is_dir": is_dir,
        "is_symlink": Path(settings.workspace_path).resolve().joinpath(configured).is_symlink()
        if configured and not Path(configured).is_absolute()
        else Path(configured).is_symlink() if configured else False,
        "real_path": str(resolved.resolve(strict=False)),
        "docker_compose_exists": compose_path.exists(),
        "package_script_exists": package_script_path.exists(),
        "package_script": str(package_script_path),
    }


@router.get("/", response_class=HTMLResponse)
def root(request: Request, current_user: OperatorUser | None = Depends(get_optional_user)):
    if current_user:
        return RedirectResponse(url="/console/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, current_user: OperatorUser | None = Depends(get_optional_user)):
    if current_user:
        return RedirectResponse(url="/console/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return render(request, "login.html", title=tr(request, None, "DeployBox 登录"), error=None)


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(),
    password: str = Form(),
    db: Session = Depends(get_db),
):
    user = authenticate_user(db, username=username.strip(), password=password)
    if not user:
        return render(
            request,
            "login.html",
            title=tr(request, None, "DeployBox 登录"),
            error=tr(request, None, "用户名或密码错误"),
        )
    request.session["user_id"] = user.id
    return RedirectResponse(url="/console/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/console/settings/lang")
def set_lang(
    request: Request,
    lang: str = Form(...),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("project.manage")),
):
    from ..i18n import SUPPORTED_LANGS
    if lang in SUPPORTED_LANGS:
        current_user.preferred_lang = lang
        db.commit()
        record_audit_log(
            db,
            actor=current_user,
            action="user.update_language",
            target_type="operator_user",
            target_id=current_user.id,
            summary=f"用户 {current_user.username} 更新了界面语言",
            detail={"preferred_lang": lang},
            request=request,
        )
    referer = request.headers.get("referer", "/console/dashboard")
    return RedirectResponse(url=referer, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/console/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("system.manage")),
):
    tab = request.query_params.get("tab", "users").strip().lower()
    if tab not in {"users", "repositories", "preferences", "system", "audit"}:
        tab = "users"
    users = list_users(db)
    repositories = list_artifact_repositories(db)
    projects = db.scalars(select(Project).order_by(Project.name.asc())).all()
    audits = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(100)).all()
    system_settings_map = get_system_settings_map(db)
    selected_user_id = request.query_params.get("user_id", "").strip()
    selected_user = next((item for item in users if str(item.id) == selected_user_id), None)
    selected_repository_id = request.query_params.get("repository_id", "").strip()
    selected_repository = next((item for item in repositories if str(item.id) == selected_repository_id), None)
    return render(
        request,
        "settings.html",
        title=tr(request, current_user, "系统设置"),
        current_user=current_user,
        settings_tab=tab,
        users=users,
        selected_user=selected_user,
        repositories=repositories,
        projects=projects,
        selected_repository=selected_repository,
        audits=audits,
        role_definitions=ROLE_DEFINITIONS,
        system_settings_map=system_settings_map,
        notice=request.query_params.get("notice"),
        error=request.query_params.get("error"),
    )


@router.post("/console/settings/users")
def save_user_form(
    request: Request,
    user_id: str | None = Form(default=None),
    username: str = Form(),
    password: str = Form(default=""),
    display_name: str = Form(default=""),
    preferred_lang: str = Form(default=""),
    is_active: str | None = Form(default=None),
    is_superuser: str | None = Form(default=None),
    role_codes: list[str] = Form(default=[]),
    scoped_role_bindings: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("system.manage")),
):
    normalized_user_id = _parse_optional_int(user_id)
    user = save_operator_user(
        db,
        user_id=normalized_user_id,
        username=username,
        password=password,
        display_name=display_name,
        preferred_lang=preferred_lang,
        is_active=is_active is not None,
        is_superuser=is_superuser is not None,
        role_codes=role_codes,
        scoped_role_bindings=scoped_role_bindings,
    )
    record_audit_log(
        db,
        actor=current_user,
        action="user.save",
        target_type="operator_user",
        target_id=user.id,
        summary=f"保存用户 {user.username}",
        detail={
            "role_bindings": [
                {"role_code": role_code, "project_id": project_id}
                for role_code, project_id in list_user_role_bindings(user)
            ],
            "is_active": user.is_active,
            "is_superuser": user.is_superuser,
        },
        request=request,
    )
    return success_redirect(f"/console/settings?tab=users&user_id={user.id}", f"用户 {user.username} 已保存")


@router.post("/console/settings/repositories")
def save_repository_form(
    request: Request,
    repository_id: str | None = Form(default=None),
    name: str = Form(),
    slug: str = Form(),
    provider: str = Form(),
    bucket_name: str = Form(),
    region: str = Form(default=""),
    endpoint: str = Form(default=""),
    custom_domain: str = Form(default=""),
    path_prefix: str = Form(default=""),
    access_key_id: str = Form(default=""),
    secret_access_key: str = Form(default=""),
    is_active: str | None = Form(default=None),
    is_default: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("system.manage")),
):
    normalized_repository_id = _parse_optional_int(repository_id)
    repository = save_artifact_repository(
        db,
        repository_id=normalized_repository_id,
        name=name,
        slug=slug,
        provider=provider,
        bucket_name=bucket_name,
        region=region,
        endpoint=endpoint,
        custom_domain=custom_domain,
        path_prefix=path_prefix,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        is_active=is_active is not None,
        is_default=is_default is not None,
    )
    record_audit_log(
        db,
        actor=current_user,
        action="artifact_repository.save",
        target_type="artifact_repository",
        target_id=repository.id,
        summary=f"保存制品仓库 {repository.name}",
        detail=artifact_repository_public_summary(repository),
        request=request,
    )
    return success_redirect(
        f"/console/settings?tab=repositories&repository_id={repository.id}",
        f"仓库 {repository.name} 已保存",
    )


@router.post("/console/settings/system")
def save_system_settings_form(
    request: Request,
    deployment_trigger_timeout_seconds: str = Form(default=""),
    deployment_poll_interval_seconds: str = Form(default=""),
    deployment_watch_timeout_seconds: str = Form(default=""),
    quality_auto_check_enabled: str | None = Form(default=None),
    quality_auto_check_interval_minutes: str = Form(default=""),
    workspace_path: str = Form(default=""),
    package_script: str = Form(default=""),
    package_artifact_public_base_url: str = Form(default=""),
    local_artifacts_path: str = Form(default=""),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("system.manage")),
):
    values = {
        "deployment_trigger_timeout_seconds": deployment_trigger_timeout_seconds.strip() or None,
        "deployment_poll_interval_seconds": deployment_poll_interval_seconds.strip() or None,
        "deployment_watch_timeout_seconds": deployment_watch_timeout_seconds.strip() or None,
        "quality_auto_check_enabled": "true" if quality_auto_check_enabled is not None else "false",
        "quality_auto_check_interval_minutes": quality_auto_check_interval_minutes.strip() or None,
        "workspace_path": workspace_path.strip() or None,
        "package_script": package_script.strip() or None,
        "package_artifact_public_base_url": package_artifact_public_base_url.strip() or None,
        "local_artifacts_path": local_artifacts_path.strip() or None,
    }
    save_system_settings(db, values)
    record_audit_log(
        db,
        actor=current_user,
        action="system_settings.save",
        target_type="system_settings",
        target_id="global",
        summary="更新系统设置",
        detail=values,
        request=request,
    )
    return success_redirect("/console/settings?tab=system", "系统设置已保存")


@router.post("/console/quality-checks/run-due")
def run_due_quality_checks_form(
    request: Request,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("system.manage")),
):
    ran = run_due_quality_checks(triggered_by=f"{current_user.username}:manual-batch")
    record_audit_log(
        db,
        actor=current_user,
        action="quality_check.run_due",
        target_type="system",
        target_id="quality_checks",
        summary="手动执行到期质量巡检",
        detail={"ran_count": ran},
        request=request,
    )
    return success_redirect("/console/dashboard", f"已执行 {ran} 个项目的质量巡检")


@router.get("/console/dashboard", response_class=HTMLResponse)
def dashboard_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(
        require_any_permission("project.manage", "build.manage", "release.manage", "audit.read")
    ),
):
    allowed_project_ids = list_accessible_project_ids(
        current_user, ["project.manage", "build.manage", "release.manage", "audit.read"]
    )
    projects_stmt = (
        select(Project)
        .options(joinedload(Project.environments), joinedload(Project.releases), joinedload(Project.build_jobs))
        .order_by(Project.updated_at.desc())
        .limit(8)
    )
    if allowed_project_ids is not None:
        if not allowed_project_ids:
            allowed_project_ids = {-1}
        projects_stmt = projects_stmt.where(Project.id.in_(allowed_project_ids))
    projects = db.execute(projects_stmt).unique().scalars().all()
    builds_stmt = select(BuildJob).order_by(BuildJob.created_at.desc()).limit(8)
    if allowed_project_ids is not None:
        builds_stmt = builds_stmt.where(BuildJob.project_id.in_(allowed_project_ids))
    recent_builds = db.scalars(builds_stmt).all()
    deployment_stmt = (
        select(Deployment)
        .options(joinedload(Deployment.project), joinedload(Deployment.environment), joinedload(Deployment.release))
        .order_by(Deployment.created_at.desc())
        .limit(8)
    )
    if allowed_project_ids is not None:
        deployment_stmt = deployment_stmt.where(Deployment.project_id.in_(allowed_project_ids))
    recent_deployments = db.execute(deployment_stmt).unique().scalars().all()
    project_count_stmt = select(func.count(Project.id))
    build_count_stmt = select(func.count(BuildJob.id))
    deployment_count_stmt = select(func.count(Deployment.id))
    running_count_stmt = select(func.count(Deployment.id)).where(
        Deployment.status.in_(["queued", "submitted", "running", "timed_out_but_running"])
    )
    if allowed_project_ids is not None:
        project_count_stmt = project_count_stmt.where(Project.id.in_(allowed_project_ids))
        build_count_stmt = build_count_stmt.where(BuildJob.project_id.in_(allowed_project_ids))
        deployment_count_stmt = deployment_count_stmt.where(Deployment.project_id.in_(allowed_project_ids))
        running_count_stmt = running_count_stmt.where(Deployment.project_id.in_(allowed_project_ids))
    quality_checks = list_recent_quality_checks(db, limit=8)
    if allowed_project_ids is not None:
        quality_checks = [item for item in quality_checks if item.project_id in allowed_project_ids]
    quality_trend_points = build_quality_trend_points(db, project_ids=allowed_project_ids, limit=8)
    quality_status_summary = build_quality_status_summary(db, project_ids=allowed_project_ids)
    governance_missing = 0
    for project in projects:
        governance = ensure_project_governance(db, project)
        if not governance.owner_user_id or not governance.release_owner_user_id:
            governance_missing += 1
    summary = {
        "projects": db.scalar(project_count_stmt) or 0,
        "builds": db.scalar(build_count_stmt) or 0,
        "deployments": db.scalar(deployment_count_stmt) or 0,
        "running_deployments": db.scalar(running_count_stmt) or 0,
        "governance_missing": governance_missing,
        "quality_passed": quality_status_summary["passed"],
        "quality_warning": quality_status_summary["warning"],
        "quality_failed": quality_status_summary["failed"],
    }
    return render(
        request,
        "dashboard.html",
        title="Dashboard",
        current_user=current_user,
        projects=projects,
        recent_builds=recent_builds,
        recent_deployments=recent_deployments,
        recent_quality_checks=quality_checks,
        quality_trend_points=quality_trend_points,
        summary=summary,
    )


@router.get("/console/projects", response_class=HTMLResponse)
def projects_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(
        require_any_permission("project.manage", "build.manage", "release.manage", "audit.read")
    ),
):
    per_page = 10
    allowed_project_ids = list_accessible_project_ids(
        current_user, ["project.manage", "build.manage", "release.manage", "audit.read"]
    )
    project_count_stmt = select(func.count(Project.id))
    if allowed_project_ids is not None:
        if not allowed_project_ids:
            allowed_project_ids = {-1}
        project_count_stmt = project_count_stmt.where(Project.id.in_(allowed_project_ids))
    project_total = db.scalar(project_count_stmt) or 0
    projects_pagination = _paginate_path(
        total=project_total,
        page=_parse_page(request.query_params.get("page")),
        per_page=per_page,
        request=request,
        base_url="/console/projects",
        param_name="page",
    )
    project_stmt = (
        select(Project)
        .options(
            joinedload(Project.environments),
            joinedload(Project.releases),
            joinedload(Project.build_jobs),
            joinedload(Project.build_config),
            joinedload(Project.default_artifact_repository),
        )
        .order_by(Project.created_at.desc())
        .offset(projects_pagination["offset"])
        .limit(per_page)
    )
    if allowed_project_ids is not None:
        project_stmt = project_stmt.where(Project.id.in_(allowed_project_ids))
    projects = db.execute(project_stmt).unique().scalars().all()
    selected_project = None
    selected_project_id = request.query_params.get("project_id", "").strip()
    if selected_project_id.isdigit():
        selected_project = next((item for item in projects if item.id == int(selected_project_id)), None)
        if not selected_project:
            selected_stmt = (
                select(Project)
                .options(
                    joinedload(Project.environments),
                    joinedload(Project.releases),
                    joinedload(Project.build_jobs),
                    joinedload(Project.build_config),
                    joinedload(Project.default_artifact_repository),
                )
                .where(Project.id == int(selected_project_id))
            )
            if allowed_project_ids is not None:
                selected_stmt = selected_stmt.where(Project.id.in_(allowed_project_ids))
            selected_project = db.execute(selected_stmt).unique().scalar_one_or_none()
    if not selected_project and projects:
        selected_project = projects[0]
    summary = {
        "projects": project_total,
        "environments": db.scalar(select(func.count(Environment.id))) or 0,
        "releases": db.scalar(select(func.count(Release.id))) or 0,
        "build_jobs": db.scalar(select(func.count(BuildJob.id))) or 0,
    }
    repositories = list_artifact_repositories(db)
    return render(
        request,
        "projects.html",
        title=tr(request, current_user, "项目与接入"),
        current_user=current_user,
        projects=projects,
        selected_project=selected_project,
        projects_pagination=projects_pagination,
        repositories=repositories,
        summary=summary,
        notice=request.query_params.get("notice"),
        error=request.query_params.get("error"),
    )


@router.post("/console/projects")
def create_project_form(
    name: str = Form(),
    slug: str = Form(),
    workspace_path: str = Form(default=""),
    image_registry_prefix: str = Form(default=""),
    default_artifact_repository_id: str | None = Form(default=None),
    description: str = Form(default=""),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("project.manage")),
):
    normalized_repository_id = _parse_optional_int(default_artifact_repository_id)
    project = Project(
        name=name.strip(),
        slug=slug.strip(),
        adapter_type="webhook_manifest_v1",
        workspace_path=workspace_path.strip() or None,
        image_registry_prefix=image_registry_prefix.strip().rstrip("/") or None,
        default_artifact_repository_id=normalized_repository_id,
        description=description.strip() or None,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    ensure_project_build_config(db, project)
    ensure_default_project_components(db, project)
    record_audit_log(
        db,
        actor=current_user,
        action="project.create",
        target_type="project",
        target_id=project.id,
        summary=f"创建项目 {project.name}",
        detail={"slug": project.slug, "default_artifact_repository_id": normalized_repository_id},
    )
    return success_redirect("/console/projects", f"项目 {project.name} 已创建")


@router.post("/console/projects/{project_id}/update")
def update_project_form(
    project_id: int,
    name: str = Form(),
    slug: str = Form(),
    workspace_path: str = Form(default=""),
    image_registry_prefix: str = Form(default=""),
    default_artifact_repository_id: str | None = Form(default=None),
    description: str = Form(default=""),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("project.manage")),
):
    normalized_repository_id = _parse_optional_int(default_artifact_repository_id)
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)
    duplicate = db.scalar(select(Project).where(Project.slug == slug.strip(), Project.id != project.id))
    if duplicate:
        return error_redirect("/console/projects", f"项目标识已存在: {slug.strip()}")
    project.name = name.strip()
    project.slug = slug.strip()
    project.workspace_path = workspace_path.strip() or None
    project.image_registry_prefix = image_registry_prefix.strip().rstrip("/") or None
    project.default_artifact_repository_id = normalized_repository_id
    project.description = description.strip() or None
    db.add(project)
    db.commit()
    record_audit_log(
        db,
        actor=current_user,
        action="project.update",
        target_type="project",
        target_id=project.id,
        summary=f"更新项目 {project.name}",
        detail={"slug": project.slug, "default_artifact_repository_id": normalized_repository_id},
    )
    return success_redirect("/console/projects", f"项目 {project.name} 已更新")


@router.post("/console/projects/{project_id}/delete")
def delete_project_form(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("project.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)
    project_name = project.name
    project_id_value = project.id
    db.delete(project)
    db.commit()
    record_audit_log(
        db,
        actor=current_user,
        action="project.delete",
        target_type="project",
        target_id=project_id_value,
        summary=f"删除项目 {project_name}",
    )
    return success_redirect("/console/projects", f"项目 {project_name} 已删除")


@router.get("/console/projects/{project_id}", response_class=HTMLResponse)
def project_detail_page(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(
        require_any_permission("project.manage", "build.manage", "release.manage", "audit.read")
    ),
):
    per_page = 10
    project = db.execute(
        select(Project)
        .options(
            joinedload(Project.environments),
            joinedload(Project.build_config).joinedload(ProjectBuildConfig.template),
            joinedload(Project.components),
            joinedload(Project.default_artifact_repository),
        )
        .where(Project.id == project_id)
    ).unique().scalar_one_or_none()
    if not project:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)

    compose_path_hint = request.query_params.get("compose_path", "docker-compose.yml").strip() or "docker-compose.yml"
    build_config = ensure_project_build_config(db, project)
    governance = ensure_project_governance(db, project)
    project_components = normalize_project_component_images(db, project)
    build_config = db.execute(
        select(ProjectBuildConfig)
        .options(joinedload(ProjectBuildConfig.template))
        .where(ProjectBuildConfig.id == build_config.id)
    ).unique().scalar_one_or_none()
    build_job_total = db.scalar(select(func.count()).select_from(BuildJob).where(BuildJob.project_id == project.id)) or 0
    deployment_total = db.scalar(select(func.count()).select_from(Deployment).where(Deployment.project_id == project.id)) or 0
    release_total = db.scalar(select(func.count()).select_from(Release).where(Release.project_id == project.id)) or 0
    build_jobs_pagination = _paginate_section(
        total=build_job_total,
        page=_parse_page(request.query_params.get("build_page")),
        per_page=per_page,
        request=request,
        project_id=project.id,
        param_name="build_page",
    )
    deployments_pagination = _paginate_section(
        total=deployment_total,
        page=_parse_page(request.query_params.get("deployment_page")),
        per_page=per_page,
        request=request,
        project_id=project.id,
        param_name="deployment_page",
    )
    releases_pagination = _paginate_section(
        total=release_total,
        page=_parse_page(request.query_params.get("release_page")),
        per_page=per_page,
        request=request,
        project_id=project.id,
        param_name="release_page",
    )
    deployments = db.execute(
        select(Deployment)
        .options(joinedload(Deployment.environment), joinedload(Deployment.release))
        .where(Deployment.project_id == project.id)
        .order_by(Deployment.created_at.desc())
        .offset(deployments_pagination["offset"])
        .limit(per_page)
    ).unique().scalars().all()
    build_jobs = db.scalars(
        select(BuildJob)
        .where(BuildJob.project_id == project.id)
        .order_by(BuildJob.created_at.desc())
        .offset(build_jobs_pagination["offset"])
        .limit(per_page)
    ).all()
    releases = db.execute(
        select(Release)
        .options(joinedload(Release.components))
        .where(Release.project_id == project.id)
        .order_by(Release.created_at.desc())
        .offset(releases_pagination["offset"])
        .limit(per_page)
    ).unique().scalars().all()
    templates_list = db.scalars(
        select(BuildTemplate).where(BuildTemplate.is_active.is_(True)).order_by(BuildTemplate.is_builtin.desc(), BuildTemplate.name.asc())
    ).all()
    repositories = list_artifact_repositories(db)
    selected_repository = get_project_artifact_repository(db, project)
    template_priority = {
        "manifest_script_v1": 0,
        "manifest_upload_v1": 10,
    }
    templates_list = sorted(
        templates_list,
        key=lambda item: (template_priority.get(item.slug, 50), item.name),
    )
    starter_environment_id = request.query_params.get("starter_environment_id", "").strip()
    selected_environment = None
    if starter_environment_id.isdigit():
        selected_environment = next((env for env in project.environments if env.id == int(starter_environment_id)), None)
    if not selected_environment and project.environments:
        selected_environment = project.environments[0]
    starter_bundle = build_starter_bundle(
        project=project,
        build_config=build_config,
        components=project_components,
        environment=selected_environment,
        lang=get_locale(request, current_user),
    )
    compose_analysis = None
    compose_analysis_error = None
    resolved_workspace = resolve_project_workspace(project)
    try:
        compose_file = (resolved_workspace / compose_path_hint).resolve()
        if compose_file.exists():
            compose_analysis = analyze_compose_release_readiness(project, project_components, compose_file)
    except Exception as exc:
        compose_analysis_error = str(exc)
    active_tab = request.query_params.get("tab", "builds").strip().lower()
    if active_tab not in {"onboarding", "builds", "advanced", "governance"}:
        active_tab = "builds"
    quality_checks = list_recent_quality_checks(db, project_id=project.id, limit=8)
    project_quality_trend_points = build_quality_trend_points(db, project_id=project.id, limit=8)
    project_quality_status_summary = build_quality_status_summary(db, project_id=project.id)
    users = list_users(db)
    return render(
        request,
        "project_detail.html",
        title=f"{project.name} - {tr(request, current_user, '项目详情')}",
        current_user=current_user,
        project=project,
        build_config=build_config,
        governance=governance,
        governance_checklist=parse_release_checklist(governance),
        build_jobs=build_jobs,
        build_job_total=build_job_total,
        build_jobs_pagination=build_jobs_pagination,
        deployments=deployments,
        deployment_total=deployment_total,
        deployments_pagination=deployments_pagination,
        releases=releases,
        release_total=release_total,
        releases_pagination=releases_pagination,
        templates_list=templates_list,
        repositories=repositories,
        selected_repository=selected_repository,
        starter_bundle=starter_bundle,
        starter_environment=selected_environment,
        project_components=project_components,
        quality_checks=quality_checks,
        project_quality_trend_points=project_quality_trend_points,
        project_quality_status_summary=project_quality_status_summary,
        users=users,
        resolved_workspace_path=str(resolved_workspace),
        compose_path_hint=compose_path_hint,
        compose_analysis=compose_analysis,
        compose_analysis_error=compose_analysis_error,
        compose_refresh=request.query_params.get("compose_refresh") == "1",
        active_tab=active_tab,
        notice=request.query_params.get("notice"),
        error=request.query_params.get("error"),
    )


@router.post("/console/projects/{project_id}/build-config")
def update_build_config_form(
    request: Request,
    project_id: int,
    template_id: int = Form(),
    config_override_json: str = Form(default=""),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("project.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)
    try:
        override = normalize_build_config_override(config_override_json)
        save_project_build_config(
            db,
            project=project,
            template_id=template_id,
            config_override_json=json.dumps(override, ensure_ascii=True) if override else "",
        )
        record_audit_log(
            db,
            actor=current_user,
            action="project.build_config.save",
            target_type="project",
            target_id=project.id,
            summary=f"更新项目 {project.name} 的接入配置",
            detail={"template_id": template_id},
            request=request,
        )
        return success_redirect(f"/console/projects/{project_id}", "接入配置已更新")
    except Exception as exc:
        return error_redirect(f"/console/projects/{project_id}", str(exc))


@router.post("/console/projects/{project_id}/governance")
def save_project_governance_form(
    request: Request,
    project_id: int,
    owner_user_id: str | None = Form(default=None),
    release_owner_user_id: str | None = Form(default=None),
    risk_level: str = Form(default="medium"),
    checklist_text: str = Form(default=""),
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("project.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)
    governance = save_project_governance(
        db,
        project=project,
        owner_user_id=_parse_optional_int(owner_user_id),
        release_owner_user_id=_parse_optional_int(release_owner_user_id),
        risk_level=risk_level,
        checklist_items=checklist_text.splitlines(),
        notes=notes,
    )
    record_audit_log(
        db,
        actor=current_user,
        action="project.governance.save",
        target_type="project",
        target_id=project.id,
        summary=f"更新项目 {project.name} 的治理配置",
        detail={
            "risk_level": governance.risk_level,
            "owner_user_id": governance.owner_user_id,
            "release_owner_user_id": governance.release_owner_user_id,
        },
        request=request,
    )
    return success_redirect(f"/console/projects/{project_id}?tab=governance", "治理配置已保存")


@router.post("/console/projects/{project_id}/quality-checks")
def run_quality_check_form(
    request: Request,
    project_id: int,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_any_permission("project.manage", "build.manage", "release.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)
    check_run = run_project_quality_check(db, project=project, triggered_by=current_user.username)
    record_audit_log(
        db,
        actor=current_user,
        action="quality_check.run",
        target_type="project",
        target_id=project.id,
        summary=f"执行项目 {project.name} 的质量检查",
        detail={"quality_check_id": check_run.id, "status": check_run.status, "score": check_run.score},
        request=request,
    )
    return success_redirect(f"/console/projects/{project_id}?tab=governance", f"质量检查已完成：{check_run.summary}")


@router.post("/console/projects/{project_id}/components")
def save_project_component_form(
    request: Request,
    project_id: int,
    component_id: str | None = Form(default=None),
    compose_path: str = Form(default="docker-compose.yml"),
    name: str = Form(),
    service_name: str = Form(),
    image: str = Form(),
    dockerfile: str = Form(default="./Dockerfile"),
    context_path: str = Form(default="."),
    tar_name_pattern: str = Form(default=""),
    build_enabled: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    default_selected: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("project.manage")),
):
    normalized_component_id = _parse_optional_int(component_id)
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)
    try:
        component = save_project_component(
            db,
            project=project,
            component_id=normalized_component_id,
            name=name,
            service_name=service_name,
            image=image,
            dockerfile=dockerfile,
            context_path=context_path,
            tar_name_pattern=tar_name_pattern,
            build_enabled=build_enabled is not None,
            enabled=enabled is not None,
            default_selected=default_selected is not None,
        )
        record_audit_log(
            db,
            actor=current_user,
            action="project.component.save",
            target_type="project_component",
            target_id=component.id,
            summary=f"保存组件 {component.name}",
            detail={"project_id": project.id, "service_name": component.service_name},
            request=request,
        )
        compose_query = quote_plus(compose_path.strip() or "docker-compose.yml")
        return success_redirect(
            f"/console/projects/{project_id}?tab=onboarding&compose_path={compose_query}&compose_refresh=1",
            "组件配置已保存",
        )
    except Exception as exc:
        compose_query = quote_plus(compose_path.strip() or "docker-compose.yml")
        return error_redirect(
            f"/console/projects/{project_id}?tab=onboarding&compose_path={compose_query}",
            str(exc),
        )


@router.post("/console/projects/{project_id}/components/{component_id}/delete")
def delete_project_component_form(
    request: Request,
    project_id: int,
    component_id: int,
    compose_path: str = Form(default="docker-compose.yml"),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("project.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)
    try:
        component = db.get(ProjectComponent, component_id)
        delete_project_component(db, project=project, component_id=component_id)
        record_audit_log(
            db,
            actor=current_user,
            action="project.component.delete",
            target_type="project_component",
            target_id=component_id,
            summary=f"删除组件 {component.name if component else component_id}",
            detail={"project_id": project.id},
            request=request,
        )
        compose_query = quote_plus(compose_path.strip() or "docker-compose.yml")
        return success_redirect(
            f"/console/projects/{project_id}?tab=onboarding&compose_path={compose_query}&compose_refresh=1",
            "组件已删除",
        )
    except Exception as exc:
        compose_query = quote_plus(compose_path.strip() or "docker-compose.yml")
        return error_redirect(
            f"/console/projects/{project_id}?tab=onboarding&compose_path={compose_query}",
            str(exc),
        )


@router.post("/console/projects/{project_id}/components/import-compose")
def import_project_components_form(
    request: Request,
    project_id: int,
    compose_path: str = Form(default="docker-compose.yml"),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("project.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)
    try:
        compose_file, imported = import_project_components_from_compose(
            db,
            project=project,
            compose_relative_path=compose_path,
        )
        record_audit_log(
            db,
            actor=current_user,
            action="project.component.import_compose",
            target_type="project",
            target_id=project.id,
            summary=f"从 compose 重新导入组件",
            detail={"compose_path": compose_path, "imported": [item.name for item in imported]},
            request=request,
        )
        return success_redirect(
            f"/console/projects/{project_id}?tab=onboarding&compose_path={quote_plus(compose_path.strip() or 'docker-compose.yml')}",
            f"已从 {compose_file.name} 导入或更新 {len(imported)} 个组件",
        )
    except Exception as exc:
        return error_redirect(
            f"/console/projects/{project_id}?tab=onboarding&compose_path={quote_plus(compose_path.strip() or 'docker-compose.yml')}",
            str(exc),
        )


@router.get("/console/projects/{project_id}/compose/recommended", response_class=PlainTextResponse)
def download_recommended_compose(
    project_id: int,
    compose_path: str = "docker-compose.yml",
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(
        require_any_permission("project.manage", "build.manage", "release.manage", "audit.read")
    ),
):
    project = db.get(Project, project_id)
    if not project:
        return PlainTextResponse("project not found", status_code=status.HTTP_404_NOT_FOUND)
    project_components = list_project_components(db, project)
    resolved_workspace = resolve_project_workspace(project)
    compose_file = (resolved_workspace / (compose_path.strip() or "docker-compose.yml")).resolve()
    if not compose_file.exists():
        return PlainTextResponse(f"compose file not found: {compose_file}", status_code=status.HTTP_404_NOT_FOUND)
    analysis = analyze_compose_release_readiness(project, project_components, compose_file)
    filename = f"{project.slug}.recommended.compose.yml"
    return PlainTextResponse(
        analysis["recommended_compose"],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/console/projects/{project_id}/environments")
def create_environment_form(
    request: Request,
    project_id: int,
    name: str = Form(),
    base_url: str = Form(default=""),
    webhook_url: str = Form(),
    status_url: str = Form(),
    shared_secret: str = Form(),
    default_environment_name: str = Form(default="prod"),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("project.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)
    environment = Environment(
        project_id=project_id,
        name=name.strip(),
        base_url=base_url.strip() or None,
        webhook_url=webhook_url.strip(),
        status_url=status_url.strip(),
        shared_secret=shared_secret.strip(),
        default_environment_name=default_environment_name.strip() or "prod",
    )
    db.add(environment)
    db.commit()
    db.refresh(environment)
    record_audit_log(
        db,
        actor=current_user,
        action="environment.create",
        target_type="environment",
        target_id=environment.id,
        summary=f"创建环境 {environment.name}",
        detail={"project_id": project_id},
        request=request,
    )
    return success_redirect(f"/console/projects/{project_id}", "环境已保存")


@router.post("/console/projects/{project_id}/environments/{environment_id}/update")
def update_environment_form(
    request: Request,
    project_id: int,
    environment_id: int,
    name: str = Form(),
    base_url: str = Form(default=""),
    webhook_url: str = Form(),
    status_url: str = Form(),
    shared_secret: str = Form(),
    default_environment_name: str = Form(default="prod"),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("project.manage")),
):
    environment = db.get(Environment, environment_id)
    if not environment or environment.project_id != project_id:
        return RedirectResponse(url=f"/console/projects/{project_id}", status_code=status.HTTP_303_SEE_OTHER)
    environment.name = name.strip()
    environment.base_url = base_url.strip() or None
    environment.webhook_url = webhook_url.strip()
    environment.status_url = status_url.strip()
    environment.shared_secret = shared_secret.strip()
    environment.default_environment_name = default_environment_name.strip() or "prod"
    db.add(environment)
    db.commit()
    record_audit_log(
        db,
        actor=current_user,
        action="environment.update",
        target_type="environment",
        target_id=environment.id,
        summary=f"更新环境 {environment.name}",
        detail={"project_id": project_id},
        request=request,
    )
    return success_redirect(f"/console/projects/{project_id}", "环境已更新")


@router.post("/console/projects/{project_id}/environments/{environment_id}/delete")
def delete_environment_form(
    request: Request,
    project_id: int,
    environment_id: int,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("project.manage")),
):
    environment = db.get(Environment, environment_id)
    if environment and environment.project_id == project_id:
        if environment.deployments:
            return error_redirect(f"/console/projects/{project_id}", "该环境已有部署记录，不能直接删除")
        env_name = environment.name
        db.delete(environment)
        db.commit()
        record_audit_log(
            db,
            actor=current_user,
            action="environment.delete",
            target_type="environment",
            target_id=environment_id,
            summary=f"删除环境 {env_name}",
            detail={"project_id": project_id},
            request=request,
        )
    return success_redirect(f"/console/projects/{project_id}", "环境已删除")


@router.post("/console/projects/{project_id}/releases")
def create_release_form(
    request: Request,
    project_id: int,
    version: str = Form(),
    manifest_url: str = Form(),
    commit: str = Form(default=""),
    payload_json: str = Form(default=""),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("release.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)
    release = Release(
        project_id=project_id,
        version=version.strip(),
        manifest_url=manifest_url.strip(),
        commit=commit.strip() or None,
        payload_json=payload_json.strip() or None,
        created_by=current_user.username,
        source_type="manual",
        storage_mode="manual",
    )
    db.add(release)
    db.commit()
    db.refresh(release)
    sync_release_manifest(db, release)
    record_audit_log(
        db,
        actor=current_user,
        action="release.create",
        target_type="release",
        target_id=release.id,
        summary=f"登记 Release {release.version}",
        detail={"project_id": project_id, "source_type": "manual"},
        request=request,
    )
    return success_redirect(f"/console/projects/{project_id}", f"Release {release.version} 已登记")


@router.post("/console/projects/{project_id}/releases/upload")
def upload_release_form(
    request: Request,
    project_id: int,
    version: str = Form(),
    commit: str = Form(default=""),
    payload_json: str = Form(default=""),
    artifact_mode: str = Form(default="auto"),
    manifest_file: UploadFile = File(),
    artifact_files: list[UploadFile] = File(),
    sha256_file: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("release.manage")),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)

    try:
        settings = get_settings()
        resolved_storage_mode, repository = resolve_project_artifact_mode(db, project=project, artifact_mode=artifact_mode)
        use_remote_storage = resolved_storage_mode != "local"

        manifest_bytes = manifest_file.file.read()
        manifest_payload = json.loads(manifest_bytes.decode("utf-8"))
        manifest_version = str(manifest_payload.get("version") or "").strip()
        version = version.strip() or manifest_version
        if not version:
            raise ValueError("版本号不能为空，且 manifest 中未包含 version")
        if manifest_version and manifest_version != version:
            raise ValueError("表单版本号与 manifest 中的 version 不一致")

        components = manifest_payload.get("components")
        if not isinstance(components, list) or not components:
            raise ValueError("manifest 中缺少有效 components")

        uploaded_artifacts: dict[str, bytes] = {}
        for artifact in artifact_files:
            artifact_name = Path(artifact.filename or "").name
            if artifact_name:
                uploaded_artifacts[artifact_name] = artifact.file.read()
        if not uploaded_artifacts:
            raise ValueError("至少需要上传一个组件 tar 文件")

        remote_prefix = f"{project.slug}/releases/{version}"
        if use_remote_storage:
            storage = build_artifact_storage(settings, repository)
            manifest_payload["artifact_storage"] = build_oss_storage_descriptor(settings, repository)
            for component in components:
                image_tar_url = str(component.get("image_tar_url") or "").strip()
                artifact_name = Path(image_tar_url).name
                artifact_bytes = uploaded_artifacts.get(artifact_name)
                if not artifact_name or artifact_bytes is None:
                    raise ValueError(f"缺少组件文件: {artifact_name or '-'}")
                remote_object_key = f"{remote_prefix}/{artifact_name}"
                component["image_tar_url"] = storage.upload_bytes(
                    data=artifact_bytes,
                    remote_path=remote_object_key,
                    content_type="application/x-tar",
                )
                component["image_tar_object_key"] = remote_object_key
            if sha256_file and sha256_file.filename:
                storage.upload_bytes(
                    data=sha256_file.file.read(),
                    remote_path=f"{remote_prefix}/sha256sum.txt",
                    content_type="text/plain; charset=utf-8",
                )
            manifest_url = storage.upload_bytes(
                data=json.dumps(manifest_payload, ensure_ascii=True, indent=2).encode("utf-8"),
                remote_path=f"{remote_prefix}/manifest.json",
                content_type="application/json; charset=utf-8",
            )
            storage_mode = resolved_storage_mode
        else:
            release_root = Path(settings.local_artifacts_path).resolve() / version
            release_root.mkdir(parents=True, exist_ok=True)
            public_base = (settings.package_artifact_public_base_url or settings.package_artifact_base_url).rstrip("/")
            manifest_payload.pop("artifact_storage", None)
            for artifact_name, artifact_bytes in uploaded_artifacts.items():
                (release_root / artifact_name).write_bytes(artifact_bytes)
            for component in components:
                image_tar_url = str(component.get("image_tar_url") or "").strip()
                artifact_name = Path(image_tar_url).name
                if not artifact_name or artifact_name not in uploaded_artifacts:
                    raise ValueError(f"缺少组件文件: {artifact_name or '-'}")
                component["image_tar_url"] = f"{public_base}/{version}/{artifact_name}"
                component.pop("image_tar_object_key", None)
            if sha256_file and sha256_file.filename:
                (release_root / "sha256sum.txt").write_bytes(sha256_file.file.read())
            (release_root / "manifest.json").write_text(
                json.dumps(manifest_payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
            manifest_url = f"{public_base}/{version}/manifest.json"
            storage_mode = "local"

        release = Release(
            project_id=project_id,
            version=version,
            manifest_url=manifest_url,
            commit=commit.strip() or None,
            payload_json=payload_json.strip() or None,
            created_by=current_user.username,
            source_type="uploaded",
            storage_mode=storage_mode,
        )
        db.add(release)
        db.commit()
        db.refresh(release)
        sync_release_manifest_payload(db, release, manifest_payload)
        record_audit_log(
            db,
            actor=current_user,
            action="release.upload",
            target_type="release",
            target_id=release.id,
            summary=f"上传并登记 Release {release.version}",
            detail={"project_id": project_id, "storage_mode": storage_mode, "repository_id": repository.id if repository else None},
            request=request,
        )
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JSONResponse({"ok": True, "redirect_url": f"/console/projects/{project_id}?notice={quote_plus(f'Release {version} 已上传并登记')}"} )
        return success_redirect(f"/console/projects/{project_id}", f"Release {version} 已上传并登记")
    except Exception as exc:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return error_redirect(f"/console/projects/{project_id}", str(exc))


@router.post("/console/projects/{project_id}/builds")
def create_build_job_form(
    request: Request,
    project_id: int,
    payload_json: str = Form(default=""),
    artifact_mode: str = Form(default="auto"),
    environment_id: str | None = Form(default=None),
    selected_component_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("build.manage")),
):
    normalized_environment_id = _parse_optional_int(environment_id)
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)
    if normalized_environment_id is not None:
        environment = db.get(Environment, normalized_environment_id)
        if not environment or environment.project_id != project.id:
            return error_redirect(f"/console/projects/{project_id}", "环境不存在或不属于当前项目")
    try:
        job = create_build_job(
            db,
            project=project,
            triggered_by=current_user.username,
            payload_json=payload_json,
            artifact_mode=artifact_mode,
            environment_id=normalized_environment_id,
            selected_component_ids=selected_component_ids,
        )
        record_audit_log(
            db,
            actor=current_user,
            action="build.create",
            target_type="build_job",
            target_id=job.id,
            summary=f"触发构建任务 #{job.id}",
            detail={"project_id": project_id, "artifact_mode": artifact_mode, "environment_id": normalized_environment_id},
            request=request,
        )
        return RedirectResponse(url=f"/console/build-jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as exc:
        return error_redirect(f"/console/projects/{project_id}", str(exc))


@router.get("/console/projects/{project_id}/starter/download")
def download_starter_bundle(
    project_id: int,
    request: Request,
    environment_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(
        require_any_permission("project.manage", "build.manage", "release.manage", "audit.read")
    ),
):
    project = db.execute(
        select(Project)
        .options(
            joinedload(Project.environments),
            joinedload(Project.build_config).joinedload(ProjectBuildConfig.template),
            joinedload(Project.components),
        )
        .where(Project.id == project_id)
    ).unique().scalar_one_or_none()
    if not project:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)
    build_config = ensure_project_build_config(db, project)
    selected_environment = None
    if environment_id is not None:
        selected_environment = next((env for env in project.environments if env.id == environment_id), None)
    if not selected_environment and project.environments:
        selected_environment = project.environments[0]
    bundle = build_starter_bundle(
        project=project,
        build_config=build_config,
        components=list_project_components(db, project),
        environment=selected_environment,
        lang=get_locale(request, current_user),
    )
    content = build_starter_archive(bundle)
    return Response(
        content=content,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{project.slug}-starter.tar.gz"'
        },
    )


@router.post("/console/projects/{project_id}/deploy")
def create_deployment_form(
    request: Request,
    project_id: int,
    environment_id: int = Form(),
    release_id: int = Form(),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("release.manage")),
):
    project = db.get(Project, project_id)
    environment = db.get(Environment, environment_id)
    release = db.get(Release, release_id)
    if not project or not environment or not release:
        return RedirectResponse(url=f"/console/projects/{project_id}", status_code=status.HTTP_303_SEE_OTHER)
    if environment.project_id != project.id or release.project_id != project.id:
        return RedirectResponse(url=f"/console/projects/{project_id}", status_code=status.HTTP_303_SEE_OTHER)
    deployment = run_deployment(
        db,
        project=project,
        environment=environment,
        release=release,
        triggered_by=current_user.username,
    )
    record_audit_log(
        db,
        actor=current_user,
        action="deployment.create",
        target_type="deployment",
        target_id=deployment.id,
        summary=f"触发部署任务 #{deployment.id}",
        detail={"project_id": project_id, "environment_id": environment_id, "release_id": release_id},
        request=request,
    )
    return RedirectResponse(url=f"/console/deployments/{deployment.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/console/build-jobs/{build_job_id}", response_class=HTMLResponse)
def build_job_detail_page(
    build_job_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(
        require_any_permission("project.manage", "build.manage", "release.manage", "audit.read")
    ),
):
    build_job = db.execute(
        select(BuildJob)
        .options(joinedload(BuildJob.project), joinedload(BuildJob.environment), joinedload(BuildJob.template))
        .where(BuildJob.id == build_job_id)
    ).unique().scalar_one_or_none()
    if not build_job:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)
    if not any(
        user_has_permission(current_user, permission, build_job.project_id)
        for permission in ("project.manage", "build.manage", "release.manage", "audit.read")
    ):
        return RedirectResponse(url="/console/projects?error=permission_denied", status_code=status.HTTP_303_SEE_OTHER)
    events = db.scalars(
        select(BuildJobEvent).where(BuildJobEvent.build_job_id == build_job.id).order_by(BuildJobEvent.created_at.asc())
    ).all()
    return render(
        request,
        "build_job_detail.html",
        title=f"{tr(request, current_user, '构建任务')} #{build_job.id}",
        current_user=current_user,
        build_job=build_job,
        events=[
            {
                "event_type": event.event_type,
                "stage": event.stage,
                "created_at": event.created_at,
                "progress_percent": event.progress_percent,
                "message": translate_runtime_text(get_translation(get_locale(request, current_user)), event.message) or "",
            }
            for event in events
        ],
        parsed_result=try_parse_json(build_job.result_json),
    )


@router.get("/console/deployments", response_class=HTMLResponse)
def deployments_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(
        require_any_permission("project.manage", "build.manage", "release.manage", "audit.read")
    ),
):
    per_page = 12
    allowed_project_ids = list_accessible_project_ids(
        current_user, ["project.manage", "build.manage", "release.manage", "audit.read"]
    )
    deployment_count_stmt = select(func.count(Deployment.id))
    if allowed_project_ids is not None:
        if not allowed_project_ids:
            allowed_project_ids = {-1}
        deployment_count_stmt = deployment_count_stmt.where(Deployment.project_id.in_(allowed_project_ids))
    deployment_total = db.scalar(deployment_count_stmt) or 0
    deployments_pagination = _paginate_path(
        total=deployment_total,
        page=_parse_page(request.query_params.get("page")),
        per_page=per_page,
        request=request,
        base_url="/console/deployments",
        param_name="page",
    )
    deployment_stmt = (
        select(Deployment)
        .options(joinedload(Deployment.project), joinedload(Deployment.environment), joinedload(Deployment.release))
        .order_by(Deployment.created_at.desc())
        .offset(deployments_pagination["offset"])
        .limit(per_page)
    )
    if allowed_project_ids is not None:
        deployment_stmt = deployment_stmt.where(Deployment.project_id.in_(allowed_project_ids))
    deployments = db.execute(deployment_stmt).unique().scalars().all()
    selected_deployment = None
    selected_deployment_id = request.query_params.get("deployment_id", "").strip()
    if selected_deployment_id.isdigit():
        selected_deployment = next((item for item in deployments if item.id == int(selected_deployment_id)), None)
    if not selected_deployment and deployments:
        selected_deployment = deployments[0]
    counts = db.execute(select(Deployment.status, func.count(Deployment.id)).group_by(Deployment.status)).all()
    summary = {status: count for status, count in counts}
    return render(
        request,
        "deployments.html",
        title=tr(request, current_user, "部署任务"),
        current_user=current_user,
        deployments=deployments,
        selected_deployment=selected_deployment,
        deployments_pagination=deployments_pagination,
        summary=summary,
        notice=request.query_params.get("notice"),
        error=request.query_params.get("error"),
    )


@router.get("/console/deployments/{deployment_id}", response_class=HTMLResponse)
def deployment_detail_page(
    deployment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(
        require_any_permission("project.manage", "build.manage", "release.manage", "audit.read")
    ),
):
    deployment = db.execute(
        select(Deployment)
        .options(
            joinedload(Deployment.project),
            joinedload(Deployment.environment),
            joinedload(Deployment.release).joinedload(Release.components),
        )
        .where(Deployment.id == deployment_id)
    ).unique().scalar_one_or_none()
    if not deployment:
        return RedirectResponse(url="/console/deployments", status_code=status.HTTP_303_SEE_OTHER)
    if not any(
        user_has_permission(current_user, permission, deployment.project_id)
        for permission in ("project.manage", "build.manage", "release.manage", "audit.read")
    ):
        return RedirectResponse(url="/console/deployments?error=permission_denied", status_code=status.HTTP_303_SEE_OTHER)
    available_releases = db.execute(
        select(Release)
        .options(joinedload(Release.components))
        .where(Release.project_id == deployment.project_id)
        .order_by(Release.created_at.desc())
        .limit(20)
    ).unique().scalars().all()
    parsed_status = try_parse_json(deployment.last_status_json)
    parsed_response = try_parse_json(deployment.adapter_response_json)
    rollback_component_map = {
        release.id: [component.name for component in sorted(release.components, key=lambda item: item.position)]
        for release in available_releases
    }
    return render(
        request,
        "deployment_detail.html",
        title=f"{tr(request, current_user, '部署')} #{deployment.id}",
        current_user=current_user,
        deployment=deployment,
        available_releases=available_releases,
        rollback_component_map=rollback_component_map,
        parsed_status=parsed_status,
        parsed_response=parsed_response,
        release_manifest=try_parse_json(deployment.release.manifest_json),
    )


@router.post("/console/deployments/{deployment_id}/refresh")
def refresh_deployment_form(
    deployment_id: int,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(
        require_any_permission("project.manage", "build.manage", "release.manage", "audit.read")
    ),
):
    deployment = db.execute(
        select(Deployment)
        .options(joinedload(Deployment.project), joinedload(Deployment.environment), joinedload(Deployment.release))
        .where(Deployment.id == deployment_id)
    ).unique().scalar_one_or_none()
    if not deployment:
        return RedirectResponse(url="/console/deployments", status_code=status.HTTP_303_SEE_OTHER)
    try:
        refresh_deployment_status(deployment, db)
        return success_redirect(f"/console/deployments/{deployment_id}", "目标状态已刷新")
    except Exception as exc:
        return error_redirect(f"/console/deployments/{deployment_id}", str(exc))


@router.post("/console/deployments/{deployment_id}/rollback")
def rollback_deployment_form(
    request: Request,
    deployment_id: int,
    release_id: int = Form(),
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("release.manage")),
):
    deployment = db.execute(
        select(Deployment)
        .options(joinedload(Deployment.project), joinedload(Deployment.environment))
        .where(Deployment.id == deployment_id)
    ).unique().scalar_one_or_none()
    release = db.get(Release, release_id)
    if not deployment or not release:
        return RedirectResponse(url="/console/deployments", status_code=status.HTTP_303_SEE_OTHER)
    if release.project_id != deployment.project_id:
        return RedirectResponse(url=f"/console/deployments/{deployment_id}", status_code=status.HTTP_303_SEE_OTHER)
    rollback = run_deployment(
        db,
        project=deployment.project,
        environment=deployment.environment,
        release=release,
        triggered_by=f"{current_user.username} (rollback)",
    )
    record_audit_log(
        db,
        actor=current_user,
        action="deployment.rollback",
        target_type="deployment",
        target_id=rollback.id,
        summary=f"按 Release {release.version} 回滚部署",
        detail={"source_deployment_id": deployment_id, "release_id": release.id},
        request=request,
    )
    return RedirectResponse(url=f"/console/deployments/{rollback.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/console/releases/{release_id}/sync-manifest")
def sync_manifest_form(
    request: Request,
    release_id: int,
    db: Session = Depends(get_db),
    current_user: OperatorUser = Depends(require_permission("release.manage")),
):
    release = db.execute(
        select(Release).options(joinedload(Release.components)).where(Release.id == release_id)
    ).unique().scalar_one_or_none()
    if not release:
        return RedirectResponse(url="/console/projects", status_code=status.HTTP_303_SEE_OTHER)
    sync_release_manifest(db, release)
    record_audit_log(
        db,
        actor=current_user,
        action="release.sync_manifest",
        target_type="release",
        target_id=release.id,
        summary=f"重新同步 Release {release.version} 的 manifest",
        request=request,
    )
    return success_redirect(f"/console/projects/{release.project_id}", "Manifest 已重新同步")
