import gettext
from ast import literal_eval
from pathlib import Path

from fastapi import Request

SUPPORTED_LANGS = {"zh", "en"}
FALLBACK_LANG = "zh"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

_cache: dict[str, gettext.GNUTranslations] = {}


class DictTranslations(gettext.NullTranslations):
    def __init__(self, catalog: dict[str, str]):
        super().__init__()
        self._catalog = catalog

    def gettext(self, message: str) -> str:
        translated = self._catalog.get(message)
        return translated if translated else message

    def ngettext(self, msgid1: str, msgid2: str, n: int) -> str:
        return self.gettext(msgid1 if n == 1 else msgid2)


def _decode_po_line(line: str) -> str:
    return literal_eval(line)


def _load_po_catalog(path: Path) -> dict[str, str]:
    catalog: dict[str, str] = {}
    msgid_parts: list[str] = []
    msgstr_parts: list[str] = []
    state: str | None = None

    def flush() -> None:
        nonlocal msgid_parts, msgstr_parts, state
        if not msgid_parts:
            return
        msgid = "".join(msgid_parts)
        msgstr = "".join(msgstr_parts)
        if msgid:
            catalog[msgid] = msgstr or msgid
        msgid_parts = []
        msgstr_parts = []
        state = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            flush()
            continue
        if line.startswith("msgid "):
            flush()
            state = "msgid"
            msgid_parts.append(_decode_po_line(line[6:]))
            continue
        if line.startswith("msgstr "):
            state = "msgstr"
            msgstr_parts.append(_decode_po_line(line[7:]))
            continue
        if line.startswith('"'):
            if state == "msgid":
                msgid_parts.append(_decode_po_line(line))
            elif state == "msgstr":
                msgstr_parts.append(_decode_po_line(line))
    flush()
    return catalog


def get_translation(lang: str) -> gettext.GNUTranslations:
    if lang not in _cache:
        try:
            t = gettext.translation("messages", localedir=str(LOCALES_DIR), languages=[lang])
        except FileNotFoundError:
            po_path = LOCALES_DIR / lang / "LC_MESSAGES" / "messages.po"
            if po_path.exists():
                t = DictTranslations(_load_po_catalog(po_path))
            else:
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


def translate_runtime_text(translation: gettext.NullTranslations, text: str | None) -> str | None:
    if text is None:
        return None
    _ = translation.gettext
    patterns = [
        ("本次将处理组件: ", lambda value: _("本次将处理组件: {components}").format(components=value)),
        ("使用模板 ", lambda value: _("使用模板 {template}").format(template=value)),
        ("工作目录不存在: ", lambda value: _("工作目录不存在: {path}").format(path=value)),
        ("打包脚本不存在: ", lambda value: _("打包脚本不存在: {path}").format(path=value)),
        ("未找到 manifest 文件: ", lambda value: _("未找到 manifest 文件: {path}").format(path=value)),
        ("未找到组件文件: ", lambda value: _("未找到组件文件: {name}").format(name=value)),
    ]
    for prefix, formatter in patterns:
        if text.startswith(prefix):
            return formatter(text.removeprefix(prefix))
    if text.startswith("Release ") and text.endswith(" 已生成"):
        version = text[len("Release ") : -len(" 已生成")]
        return _("Release {version} 已生成").format(version=version)
    if text.startswith("组件 ") and text.endswith(" 为外部镜像，跳过 tar 上传"):
        component = text[len("组件 ") : -len(" 为外部镜像，跳过 tar 上传")]
        return _("组件 {component} 为外部镜像，跳过 tar 上传").format(component=component)
    if text.startswith("已上传组件 ") and ": " in text:
        prefix, name = text[len("已上传组件 ") :].split(": ", 1)
        current, total = prefix.split("/", 1)
        return _("已上传组件 {current}/{total}: {name}").format(current=current, total=total, name=name)
    return _(text)
