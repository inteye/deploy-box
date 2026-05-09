from __future__ import annotations

import gettext
from ast import literal_eval
from pathlib import Path

from fastapi import Request

SUPPORTED_LANGS = {"zh", "en"}
FALLBACK_LANG = "zh"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

_cache: dict[str, gettext.GNUTranslations] = {}
_cache_mtime: dict[str, float] = {}


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
    po_path = LOCALES_DIR / lang / "LC_MESSAGES" / "messages.po"
    current_mtime = po_path.stat().st_mtime if po_path.exists() else 0
    if lang not in _cache or _cache_mtime.get(lang, 0) != current_mtime:
        try:
            t = gettext.translation("messages", localedir=str(LOCALES_DIR), languages=[lang])
        except FileNotFoundError:
            if po_path.exists():
                t = DictTranslations(_load_po_catalog(po_path))
            else:
                t = gettext.NullTranslations()
        _cache[lang] = t
        _cache_mtime[lang] = current_mtime
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


def _split_counter(text):
    parts = text.split("/", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return text, ""


def _translate_parametric(_, text):
    parametric_patterns = [
        ("本次将处理组件: ", lambda v: _("本次将处理组件: {components}").format(components=v)),
        ("使用模板 ", lambda v: _("使用模板 {template}").format(template=_(v))),
        ("工作目录不存在: ", lambda v: _("工作目录不存在").format() + f": {v}"),
        ("打包脚本不存在: ", lambda v: _("打包脚本不存在").format() + f": {v}"),
        ("未找到 manifest 文件: ", lambda v: _("未找到 manifest 文件").format() + f": {v}"),
        ("未找到组件文件: ", lambda v: _("未找到组件文件").format() + f": {v}"),
        ("组件名已存在: ", lambda v: _("组件名已存在").format() + f": {v}"),
        ("Compose service 已存在: ", lambda v: _("Compose service 已存在").format() + f": {v}"),
        ("Compose 文件不存在: ", lambda v: _("Compose 文件不存在").format() + f": {v}"),
        ("工作目录不存在: ", lambda v: _("工作目录不存在").format() + f": {v}"),
        ("打包脚本不存在: ", lambda v: _("打包脚本不存在").format() + f": {v}"),
        ("未找到 manifest 文件: ", lambda v: _("未找到 manifest 文件").format() + f": {v}"),
        ("未找到组件文件: ", lambda v: _("未找到组件文件").format() + f": {v}"),
        ("触发发布失败: ", lambda v: _("触发发布失败").format() + f": {v}"),
        ("轮询状态失败: ", lambda v: _("轮询状态失败").format() + f": {v}"),
        ("工作区不可用: ", lambda v: _("工作区不可用").format() + f": {v}"),
        ("触发发布失败: HTTP ", lambda v: _("触发发布失败").format() + f": HTTP {v}"),
        ("轮询状态失败: ", lambda v: _("轮询状态失败").format() + f": {v}"),
        ("当前仅支持 manifest_script_v1，实际为 ", lambda v: _("当前仅支持 manifest_script_v1，实际为").format() + f" {v}"),
    ]
    for prefix, formatter in parametric_patterns:
        if text.startswith(prefix):
            return formatter(text[len(prefix):])

    if text.startswith("Release ") and text.endswith(" 已生成"):
        version = text[len("Release "):-len(" 已生成")]
        return _("Release {version} 已生成").format(version=version)

    if text.startswith("组件 ") and text.endswith(" 为外部镜像，跳过 tar 上传"):
        component = text[len("组件 "):-len(" 为外部镜像，跳过 tar 上传")]
        return _("组件 {component} 为外部镜像，跳过 tar 上传").format(component=component)

    for build_prefix in ("正在构建组件 ", "已完成组件 ", "已上传组件 "):
        if text.startswith(build_prefix) and ": " in text:
            counter, name = text[len(build_prefix):].split(": ", 1)
            current, total = _split_counter(counter)
            key_map = {
                "正在构建组件 ": "正在构建组件 {current}/{total}: {name}",
                "已完成组件 ": "已完成组件 {current}/{total}: {name}",
                "已上传组件 ": "已上传组件 {current}/{total}: {name}",
            }
            return _(key_map[build_prefix]).format(current=current, total=total, name=name)

    if text.startswith("已完成外部镜像 ") and ": " not in text:
        counter, name = text[len("已完成外部镜像 "):].split("/", 1) if "/" in text[len("已完成外部镜像 "):] else (text[len("已完成外部镜像 "):], "")
        if "/" in text[len("已完成外部镜像 "):]:
            parts = text[len("已完成外部镜像 "):].split(" ", 1)
            if len(parts) == 2:
                counter_part, name = parts[0], parts[1]
                c, t = _split_counter(counter_part)

    if text.startswith("已维护 ") and " 条发布检查项" in text:
        count = text[len("已维护 "):text.index(" 条发布检查项")]
        return _("已维护发布检查项").format() + f" ({count})"

    if text.startswith("当前启用 ") and " 个组件" in text:
        count = text[len("当前启用 "):text.index(" 个组件")]
        return _("当前启用了个组件").format() + f" ({count})"

    if text.startswith("当前有 ") and " 个构建型组件" in text:
        count = text[len("当前有 "):text.index(" 个构建型组件")]
        return _("当前有个构建型组件").format() + f" ({count})"

    if text.startswith("已配置 ") and " 个环境" in text:
        count = text[len("已配置 "):text.index(" 个环境")]
        return _("已配置了个环境").format() + f" ({count})"

    if text.startswith("自动执行项目 ") and " 的质量检查" in text:
        name = text[len("自动执行项目 "):text.index(" 的质量检查")]
        return _("自动执行项目的质量检查").format() + f" {name}"

    compose_prefixes = (
        ("service `", lambda rest: _translate_compose_service(_, rest)),
    )
    for cp, handler in compose_prefixes:
        if text.startswith(cp):
            return handler(text[len(cp):])

    failed_match = text.endswith(" 项失败，") or (" 项失败，" in text and " 项警告" in text)
    if failed_match:
        import re
        m = re.match(r"(\d+) 项失败，(\d+) 项警告", text)
        if m:
            return f"{m.group(1)} {_('项失败，项警告').format()} {m.group(2)}"
        m = re.match(r"(\d+) 项失败，(\d+) 项警告", text)
        if m:
            return f"{m.group(1)} {m.group(2)}"

    if text.endswith(" 项警告，建议修正后再发布"):
        import re
        m = re.match(r"(\d+) 项警告，建议修正后再发布", text)
        if m:
            return f"{m.group(1)} {_('项警告，建议修正后再发布').format()}"

    return None


def _translate_compose_service(_, rest):
    if "` 同时存在 build 配置" in rest:
        name = rest[:rest.index("` 同时存在 build 配置")]
        return f"service `{name}` {_('同时存在 build 配置').format()}"
    if "` 只有 build 没有 image" in rest:
        name = rest[:rest.index("` 只有 build 没有 image")]
        return f"service `{name}` {_('只有 build 没有 image').format()}"
    if "` 使用 latest 标签" in rest:
        name = rest[:rest.index("` 使用 latest 标签")]
        return f"service `{name}` {_('使用 latest 标签').format()}"
    if "` 未接入 " in rest:
        name = rest[:rest.index("` 未接入 ")]
        env_key = rest[rest.index("` 未接入 ") + len("` 未接入 "):]
        return f"service `{name}` {_('未接入').format()} {env_key}"
    if "` 存在源码目录挂载" in rest:
        name = rest[:rest.index("` 存在源码目录挂载")]
        return f"service `{name}` {_('存在源码目录挂载').format()}"
    if "` 已为组件" in rest and "追加推荐 service" in rest:
        return _(rest)
    return None


def translate_runtime_text(translation: gettext.NullTranslations, text: str | None) -> str | None:
    if text is None:
        return None
    _ = translation.gettext
    result = _translate_parametric(_, text)
    if result is not None:
        return result
    return _(text)
