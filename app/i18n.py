import gettext
from pathlib import Path

from fastapi import Request

SUPPORTED_LANGS = {"zh", "en"}
FALLBACK_LANG = "zh"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

_cache: dict[str, gettext.GNUTranslations] = {}


def get_translation(lang: str) -> gettext.GNUTranslations:
    if lang not in _cache:
        try:
            t = gettext.translation("messages", localedir=str(LOCALES_DIR), languages=[lang])
        except FileNotFoundError:
            t = gettext.NullTranslations()
        _cache[lang] = t
    return _cache[lang]


def get_locale(request: Request, user=None) -> str:
    if user and getattr(user, "preferred_lang", None) in SUPPORTED_LANGS:
        return user.preferred_lang
    accept = request.headers.get("accept-language", "")
    for part in accept.split(","):
        lang = part.strip().split(";")[0].split("-")[0].lower()
        if lang in SUPPORTED_LANGS:
            return lang
    return FALLBACK_LANG
