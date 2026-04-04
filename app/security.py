import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


def _build_fernet() -> Fernet:
    secret = get_settings().secret_encryption_key.strip() or get_settings().secret_key.strip()
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _build_fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str) -> str:
    try:
        return _build_fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("invalid_encrypted_secret") from exc
