"""路径解析与文件投递辅助（办公工具箱）。

这里的规则和 dsh-remote-bridge 的 `files.py` 保持一致：
只处理真实存在、是普通文件、并且不在敏感目录里的路径。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

DENY_PARTS = {
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".git",
    "cookies",
    "cookies.sqlite",
    "id_rsa",
    "id_dsa",
    "id_ed25519",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "login data",
    "web data",
}

DENY_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".kdbx"}

INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class PathError(Exception):
    """路径不可用。"""


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def sanitize_name(name: str, *, default: str = "未命名") -> str:
    """把用户给的名称变成安全文件名（保留中文）。"""

    text = (name or "").strip().strip(".")
    text = INVALID_NAME.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        text = default
    return text[:120]


def unique_path(directory: Path, name: str) -> Path:
    """同名文件自动加序号，避免覆盖。"""

    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem}({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise PathError("同名文件太多，换个名字吧")


def parse_index(token: str) -> Optional[int]:
    """把 ``#3`` / ``3`` 解析成 1 基序号；不是序号返回 None。"""

    text = (token or "").strip().lstrip("#")
    if text.isdigit():
        value = int(text)
        return value if value > 0 else None
    return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def ensure_allowed(path: Path, allowed_roots: Sequence[str]) -> None:
    """校验路径不在敏感目录、且在允许的根目录内。"""

    resolved = path.resolve()
    parts = {part.casefold() for part in resolved.parts}
    if parts & {item.casefold() for item in DENY_PARTS}:
        raise PathError("这个路径在敏感目录里，出于安全考虑不允许操作")
    if resolved.suffix.casefold() in DENY_SUFFIXES:
        raise PathError("这个类型的凭据文件不允许操作")

    roots = [root for root in (allowed_roots or []) if str(root).strip()]
    if roots:
        for root in roots:
            try:
                root_path = Path(str(root)).expanduser().resolve()
            except OSError:
                continue
            if _is_within(resolved, root_path):
                return
        raise PathError("这个路径不在允许的目录里（检查 storage.allowed_roots）")


def resolve_user_path(raw: str, *, workspace: Path, allowed_roots: Sequence[str]) -> Path:
    """把用户输入解析成绝对路径并做安全校验。"""

    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        raise PathError("请给出文件路径")
    if text.startswith("file:///"):
        text = text[8:]
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise PathError(f"路径无法解析：{exc}") from exc
    ensure_allowed(resolved, allowed_roots)
    return resolved


def file_meta(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size": stat.st_size,
        "size_text": human_size(stat.st_size),
        "mtime": stat.st_mtime,
        "suffix": path.suffix.lower(),
    }


def to_file_payload(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Host 侧 ``file`` 消息段需要的负载。"""

    path = Path(str(meta.get("path", "")))
    return {
        "name": str(meta.get("name") or path.name),
        "size": str(meta.get("size") or ""),
        "mime_type": str(meta.get("mime") or ""),
        "url": path.as_uri(),
    }


def list_dir(directory: Path, *, keyword: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    """列出目录下的文件（不含子目录内容）。"""

    if not directory.is_dir():
        return []
    keyword_cf = (keyword or "").casefold()
    items: List[Dict[str, Any]] = []
    for child in sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if not child.is_file():
            continue
        if keyword_cf and keyword_cf not in child.name.casefold():
            continue
        try:
            items.append(file_meta(child))
        except OSError:
            continue
        if len(items) >= limit:
            break
    return items
