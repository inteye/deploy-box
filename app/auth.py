import base64
import hashlib
import hmac
import os
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import OperatorUser, OperatorUserRole


ROLE_DEFINITIONS: dict[str, dict[str, object]] = {
    "system_admin": {
        "name": "系统管理员",
        "permissions": {"system.manage", "audit.read", "project.manage", "build.manage", "release.manage"},
    },
    "onboarding_admin": {
        "name": "接入管理员",
        "permissions": {"project.manage"},
    },
    "build_admin": {
        "name": "构建管理员",
        "permissions": {"build.manage"},
    },
    "release_admin": {
        "name": "发布管理员",
        "permissions": {"release.manage"},
    },
    "audit_viewer": {
        "name": "审计查看者",
        "permissions": {"audit.read"},
    },
}


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return base64.b64encode(salt + digest).decode("ascii")


def verify_password(password: str, encoded: str) -> bool:
    raw = base64.b64decode(encoded.encode("ascii"))
    salt, expected = raw[:16], raw[16:]
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return hmac.compare_digest(expected, digest)


def bootstrap_admin(db: Session) -> None:
    settings = get_settings()
    user = db.scalar(select(OperatorUser).where(OperatorUser.username == settings.admin_username))
    if user:
        ensure_user_roles(db, user, ["system_admin"])
        return
    user = OperatorUser(
        username=settings.admin_username,
        password_hash=hash_password(settings.admin_password),
        is_active=True,
        is_superuser=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    ensure_user_roles(db, user, ["system_admin"])


def authenticate_user(db: Session, username: str, password: str) -> OperatorUser | None:
    user = db.scalar(select(OperatorUser).where(OperatorUser.username == username, OperatorUser.is_active.is_(True)))
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.now(timezone.utc)
    db.add(user)
    db.commit()
    return user


def get_current_user(request: Request, db: Session = Depends(get_db)) -> OperatorUser:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login_required")
    user = db.get(OperatorUser, user_id)
    if not user or not user.is_active:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login_required")
    return user


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> OperatorUser | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get(OperatorUser, user_id)
    if not user or not user.is_active:
        request.session.clear()
        return None
    return user


def list_user_role_codes(user: OperatorUser) -> list[str]:
    return sorted({assignment.role_code for assignment in getattr(user, "role_assignments", []) if assignment.role_code})


def list_user_role_bindings(user: OperatorUser) -> list[tuple[str, int | None]]:
    return sorted(
        {
            (assignment.role_code, assignment.project_id)
            for assignment in getattr(user, "role_assignments", [])
            if assignment.role_code
        },
        key=lambda item: (item[0], item[1] or 0),
    )


def list_accessible_project_ids(user: OperatorUser, permissions: list[str]) -> set[int] | None:
    if user.is_superuser:
        return None
    allowed: set[int] = set()
    for assignment in getattr(user, "role_assignments", []):
        role = ROLE_DEFINITIONS.get(assignment.role_code) or {}
        role_permissions = set(role.get("permissions") or [])
        if not role_permissions.intersection(permissions):
            continue
        if assignment.project_id is None:
            return None
        allowed.add(assignment.project_id)
    return allowed


def ensure_user_roles(db: Session, user: OperatorUser, role_bindings: list[str | tuple[str, int | None]]) -> None:
    desired: set[tuple[str, int | None]] = set()
    for item in role_bindings:
        if isinstance(item, tuple):
            role_code, project_id = item
        else:
            role_code, project_id = item, None
        if role_code in ROLE_DEFINITIONS:
            desired.add((role_code, project_id))
    existing = db.scalars(select(OperatorUserRole).where(OperatorUserRole.user_id == user.id)).all()
    existing_bindings = {(item.role_code, item.project_id) for item in existing}
    for item in existing:
        if (item.role_code, item.project_id) not in desired:
            db.delete(item)
    for role_code, project_id in desired - existing_bindings:
        db.add(OperatorUserRole(user_id=user.id, role_code=role_code, project_id=project_id))
    db.commit()


def user_has_permission(user: OperatorUser, permission: str, project_id: int | None = None) -> bool:
    if user.is_superuser:
        return True
    for assignment in getattr(user, "role_assignments", []):
        role = ROLE_DEFINITIONS.get(assignment.role_code) or {}
        permissions = set(role.get("permissions") or [])
        if permission not in permissions:
            continue
        if project_id is None or assignment.project_id is None or assignment.project_id == project_id:
            return True
    return False


def require_permission(permission: str):
    def dependency(request: Request, current_user: OperatorUser = Depends(get_current_user)) -> OperatorUser:
        if not user_has_permission(current_user, permission):
            raw_project_id = request.path_params.get("project_id")
            if raw_project_id and str(raw_project_id).isdigit():
                if user_has_permission(current_user, permission, int(raw_project_id)):
                    return current_user
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="permission_denied")
        return current_user

    return dependency


def require_any_permission(*permissions: str):
    def dependency(request: Request, current_user: OperatorUser = Depends(get_current_user)) -> OperatorUser:
        if current_user.is_superuser:
            return current_user
        raw_project_id = request.path_params.get("project_id")
        project_id = int(raw_project_id) if raw_project_id and str(raw_project_id).isdigit() else None
        for permission in permissions:
            if user_has_permission(current_user, permission) or (
                project_id is not None and user_has_permission(current_user, permission, project_id)
            ):
                return current_user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="permission_denied")

    return dependency
