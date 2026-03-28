import base64
import hashlib
import hmac
import os

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import OperatorUser


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
        return
    user = OperatorUser(
        username=settings.admin_username,
        password_hash=hash_password(settings.admin_password),
        is_active=True,
        is_superuser=True,
    )
    db.add(user)
    db.commit()


def authenticate_user(db: Session, username: str, password: str) -> OperatorUser | None:
    user = db.scalar(select(OperatorUser).where(OperatorUser.username == username, OperatorUser.is_active.is_(True)))
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
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
