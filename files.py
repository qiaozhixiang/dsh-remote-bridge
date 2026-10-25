"""从 DSH 回复里识别「生成的文件」并按地址投递到当前对话。

DSH 的回答本身只是一段文本，但它调用工具（写文件、导出图片、编译产物）时，
路径会出现在 assistant 消息、tool-call 参数和 tool-result 正文里。本模块负责：

1. 把这些字符串里出现的**绝对路径**挖出来；
2. 只保留真实存在、大小合规、不在排除目录里的普通文件；
3. 交给插件按 ``file`` 消息段发到当前对话窗口。
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Windows 盘符路径、UNC 路径、POSIX 绝对路径、file:// URI
_WIN_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'<>|?*\r\n\t]+")
_POSIX_PATH = re.compile(r"(?<![\w:.])/(?:[\w.\-]+/)+[\w.\-]+")
_FILE_URI = re.compile(r"file:///[^\s\"'<>|?*\r\n\t]+", re.IGNORECASE)

# 路径末尾常见的标点不属于文件名
_TRAILING = " \t\r\n,.;:!?)]}>'\"`，。；：！？）】》」"

# 永远不投递的目录（避免把凭据/缓存发出去）
_DENY_PARTS = {
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".git",
    "__pycache__",
    "node_modules",
    "cookies",
    "login data",
}

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _iter_strings(value: Any, depth: int = 0) -> Iterable[str]:
    """递归取出结构里所有字符串。"""

    if depth > 12:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item, depth + 1)


def iter_candidates(text: str) -> Iterable[str]:
    """从一段文本里挖出可能是文件路径的片段。"""

    if not text or len(text) > 200_000:
        return
    for pattern in (_FILE_URI, _WIN_PATH, _POSIX_PATH):
        for match in pattern.finditer(text):
            raw = match.group(0).rstrip(_TRAILING)
            if not raw:
                continue
            if raw.lower().startswith("file:///"):
                raw = raw[8:]
            yield raw


def normalize_path(raw: str) -> Optional[Path]:
    """把候选字符串变成存在的文件路径；不是文件就返回 None。"""

    text = (raw or "").strip()
    if not text or len(text) > 4096:
        return None
    # 去掉 DSH/工具输出里常见的行号后缀  path:12:5
    text = re.sub(r":\d+(?::\d+)?$", "", text)
    try:
        path = Path(text)
    except (OSError, ValueError):
        return None
    try:
        if not path.is_absolute():
            return None
        if not path.exists() or not path.is_file():
            return None
        return path.resolve()
    except (OSError, ValueError):
        return None


def is_denied(path: Path) -> bool:
    """命中凭据/缓存目录的一律不投递。"""

    parts = {part.casefold() for part in path.parts}
    return bool(parts & _DENY_PARTS)


def file_meta(path: Path) -> Dict[str, Any]:
    """构造给前端/消息层用的文件描述。"""

    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    mime, _ = mimetypes.guess_type(path.name)
    return {
        "name": path.name,
        "path": str(path),
        "size": size,
        "mime": mime or "application/octet-stream",
        "image": path.suffix.lower() in _IMAGE_SUFFIXES,
    }


def _record_seq(record: Any, fallback: int) -> int:
    event = record.get("event") if isinstance(record, dict) else None
    if isinstance(event, dict):
        seq = event.get("seq")
        if isinstance(seq, int):
            return seq
    return fallback


def collect_files(
    records: Sequence[Any],
    *,
    since_seq: int = -1,
    max_files: int = 5,
    max_bytes: int = 25 * 1024 * 1024,
    allowed_roots: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """从一批 DSH records 里挑出可投递的文件（去重、保序、限量）。"""

    roots: List[Path] = []
    for item in allowed_roots:
        try:
            roots.append(Path(item).expanduser().resolve())
        except (OSError, ValueError):
            continue

    found: List[Dict[str, Any]] = []
    seen: set[str] = set()
    index = 0
    for record in records or []:
        index += 1
        if _record_seq(record, index) <= since_seq:
            continue
        event = record.get("event") if isinstance(record, dict) else None
        if not isinstance(event, dict):
            continue
        if event.get("type") not in {"assistant/message", "tool/result", "tool/call", "system/message"}:
            continue
        for text in _iter_strings(event.get("data")):
            for candidate in iter_candidates(text):
                path = normalize_path(candidate)
                if path is None:
                    continue
                key = str(path).casefold()
                if key in seen or is_denied(path):
                    continue
                if roots and not any(_is_within(path, root) for root in roots):
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size <= 0 or size > max_bytes:
                    continue
                seen.add(key)
                meta = file_meta(path)
                meta["seq"] = _record_seq(record, index)
                found.append(meta)
                if len(found) >= max_files:
                    return found
    return found


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def to_file_payload(meta: Dict[str, Any]) -> Dict[str, Any]:
    """转成 Host 侧 ``file`` 消息段需要的负载。"""

    path = Path(str(meta.get("path", "")))
    return {
        "name": str(meta.get("name") or path.name),
        "size": str(meta.get("size") or ""),
        "mime_type": str(meta.get("mime") or ""),
        "url": path.as_uri(),
    }


def read_base64(meta: Dict[str, Any], *, limit: int = 8 * 1024 * 1024) -> Optional[str]:
    """读成 base64；超过 limit 返回 None。"""

    path = Path(str(meta.get("path", "")))
    try:
        if path.stat().st_size > limit:
            return None
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None


def human_size(size: int) -> str:
    """人类可读的大小。"""

    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def summarize(files: Sequence[Dict[str, Any]], limit: int = 5) -> str:
    """一行式摘要，用于 QQ 文本提示。"""

    items = list(files)[:limit]
    if not items:
        return ""
    return "、".join(f"{item['name']}（{human_size(int(item.get('size') or 0))}）" for item in items)


def describe_for_prompt(files: Sequence[Dict[str, Any]]) -> str:
    """给前端展示用的紧凑结构。"""

    return "\n".join(f"{item['name']}\t{human_size(int(item.get('size') or 0))}\t{item['path']}" for item in files)


def _resolve_roots(allowed_roots: Sequence[str]) -> List[Path]:
    roots: List[Path] = []
    for item in allowed_roots or []:
        try:
            roots.append(Path(str(item)).expanduser().resolve())
        except (OSError, ValueError):
            continue
    return roots


def is_allowed_root(path: Path, allowed_roots: Sequence[str]) -> bool:
    """文件是否落在 ``files.allowed_roots`` 允许的目录内；留空表示不限制。"""

    roots = _resolve_roots(allowed_roots)
    if not roots:
        return True
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    return any(_is_within(resolved, root) for root in roots)
