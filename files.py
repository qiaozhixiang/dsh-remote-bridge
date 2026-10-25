"""从 DSH 回复里识别「生成的文件」并按地址投递到当前对话。

DSH 的回答本身只是一段文本，但它调用工具（写文件、导出图片、编译产物）时，
路径会出现在 assistant 消息、tool-call 参数和 tool-result 正文里。本模块负责：

1. 把这些字符串里出现的**绝对路径**挖出来；
2. 只保留真实存在、大小合规、不在排除目录里的普通文件；
3. 交给插件按 ``file`` 消息段发到当前对话窗口。
"""

from __future__ import annotations

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
    ".kube",
    ".docker",
    ".gcloud",
    ".password-store",
    ".mozilla",
    ".thunderbird",
    ".config",
    ".git",
    ".hg",
    ".svn",
    ".aws-sso",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "site-packages",
    "cookies",
    "login data",
}

# 系统目录：命中这些前缀的一律不投递
_DENY_PREFIXES = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/root",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/var/lib",
    "/private/etc",
    "/private/var/db",
)

# Windows 系统盘目录
_DENY_WIN_PREFIX = re.compile(
    r"^[a-z]:/(?:windows|program files|program files \(x86\)|programdata)(?:/|$)"
)

# 文件名/后缀黑名单：凭据、密钥、浏览器保存的密码、shell 历史等
_DENY_NAME = re.compile(
    r"(?:^\.?netrc$"
    r"|^\.npmrc$"
    r"|^\.pypirc$"
    r"|^\.pgpass$"
    r"|^\.git-credentials$"
    r"|^\.htpasswd$"
    r"|^id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?$"
    r"|^\.env(?:\..*)?$"
    r"|^\.bash_history$"
    r"|^\.zsh_history$"
    r"|^\.psql_history$"
    r"|^credentials(?:\.json)?$"
    r"|^config\.json$"
    r"|^logins\.json$"
    r"|^key[34]\.db$"
    r"|^cookies\.sqlite$"
    r"|^local state$"
    r"|token|secret|passwd|password|credential"
    r"|\.pem$|\.key$|\.pfx$|\.p12$|\.jks$|\.keystore$|\.kdbx$|\.ppk$)"
)

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
    """命中凭据/缓存/系统目录或敏感文件名的一律不投递。"""

    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        resolved = Path(path)

    parts = {part.casefold() for part in resolved.parts}
    if parts & _DENY_PARTS:
        return True

    text = str(resolved).replace("\\", "/").casefold()
    variants = [text]
    stripped = re.sub(r"^[a-z]:/", "", text)
    if stripped != text:
        # Windows 上 "/etc/shadow" 这类写法会解析成 "C:/etc/shadow"，同样按系统路径处理
        variants.append("/" + stripped)
    for candidate in variants:
        for prefix in _DENY_PREFIXES:
            if candidate == prefix or candidate.startswith(prefix + "/"):
                return True
    if _DENY_WIN_PREFIX.match(text):
        return True

    name = resolved.name.casefold()
    if _DENY_NAME.search(name):
        return True

    # 用户主目录下的点文件（.bashrc、.gitconfig 等）一律不投递
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    if home is not None and resolved.parent == home and name.startswith("."):
        return True
    return False


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
    fallback_roots: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """从一批 DSH records 里挑出可投递的文件（去重、保序、限量）。

    ``allowed_roots`` 为空时使用 ``fallback_roots``（插件数据目录）；
    两者都为空则不投递任何文件。
    """

    roots = _resolve_roots(allowed_roots) or _resolve_roots(fallback_roots)
    if not roots:
        return []

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
                if not any(_is_within(path, root) for root in roots):
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


def human_size(size: int) -> str:
    """人类可读的大小。"""

    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _resolve_roots(*groups: Sequence[str]) -> List[Path]:
    roots: List[Path] = []
    for group in groups:
        for item in group or []:
            try:
                roots.append(Path(str(item)).expanduser().resolve())
            except (OSError, ValueError, RuntimeError):
                continue
    return roots


def is_allowed_root(
    path: Path,
    allowed_roots: Sequence[str],
    *,
    fallback_roots: Sequence[str] = (),
) -> bool:
    """文件是否落在 ``files.allowed_roots`` 允许的目录内。

    留空时退回到 ``fallback_roots``（插件数据目录）；两者都为空则默认拒绝，
    宁可少投递，也不把任意本机文件发出去。
    """

    roots = _resolve_roots(allowed_roots) or _resolve_roots(fallback_roots)
    if not roots:
        return False
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        return False
    return any(_is_within(resolved, root) for root in roots)
