"""聊天文件收件箱：把群聊/私聊里发来的文件落盘、建索引、去重。

设计要点
--------
* 只依赖标准库（urllib / json / hashlib），不引入第三方下载库。
* 落盘目录：``<data_dir>/inbox/<YYYYMMDD>/<sha8>_<安全文件名>``。
* 索引文件：``<data_dir>/inbox/index.json``，记录来源、发送者、会话、时间、大小、哈希。
* 同一内容（sha256 相同）只存一份，重复发送只更新 ``seen`` 计数与最后时间。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024
"""单个文件下载上限（字节）。"""

_FILE_TEXT_RE = re.compile(
    r"\[文件\]\s*(?P<name>[^，,]+?)\s*(?:[，,]\s*大小:\s*(?P<size>\d+))?\s*(?:[，,]\s*链接:\s*(?P<url>\S+))?",
    re.IGNORECASE,
)

_SAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, *, fallback: str = "file") -> str:
    """把任意文件名收敛成 Windows 可用的安全名。"""

    base = Path(str(name or "")).name.strip() or fallback
    base = _SAFE_NAME_RE.sub("_", base)
    base = base.strip(" .") or fallback
    if len(base) > 120:
        stem, suffix = os.path.splitext(base)
        base = stem[: 120 - len(suffix)] + suffix
    return base


def human_size(size: int) -> str:
    """人类可读的文件大小。"""

    value = float(max(0, int(size or 0)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def parse_file_text(text: str) -> List[Dict[str, str]]:
    """从消息文本里解析出 ``[文件] 名称，大小: N，链接: URL`` 形式的附件。

    QQ 适配器会把文件消息转成这种文本段，因此这里用正则回收。
    """

    found: List[Dict[str, str]] = []
    for match in _FILE_TEXT_RE.finditer(str(text or "")):
        name = (match.group("name") or "").strip()
        url = (match.group("url") or "").strip()
        size = (match.group("size") or "").strip()
        if not name and not url:
            continue
        # 没有链接时也可能是纯文本里的字面量，跳过以免误判
        if not url and not size:
            continue
        found.append({"name": name or "未命名文件", "url": url, "size": size})
    return found


def _guess_mime(name: str) -> str:
    mime, _ = mimetypes.guess_type(name)
    return mime or "application/octet-stream"


@dataclass
class InboxItem:
    """收件箱里的一条记录。"""

    item_id: str
    name: str
    stored_name: str
    rel_path: str
    size: int
    sha256: str
    mime_type: str
    source: str = "chat"
    sender_id: str = ""
    sender_name: str = ""
    session_id: str = ""
    chat_type: str = ""
    message_id: str = ""
    origin_url: str = ""
    created_at: float = 0.0
    seen: int = 1
    note: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class InboxStore:
    """收件箱的落盘与索引。线程/协程内串行使用即可。"""

    def __init__(self, root: Path, *, max_items: int = 2000) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.max_items = max(50, int(max_items or 2000))
        self._items: List[InboxItem] = []
        self._loaded = False

    # ------------------------------------------------------------------ 索引
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.index_path.exists():
            return
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        items: List[InboxItem] = []
        for entry in raw.get("items") or []:
            try:
                items.append(InboxItem(**entry))
            except TypeError:
                continue
        self._items = items

    def _save(self) -> None:
        if len(self._items) > self.max_items:
            # 只裁剪索引，磁盘文件保留（用户可能还要用）
            self._items = self._items[-self.max_items :]
        payload = {"version": 1, "items": [item.to_dict() for item in self._items]}
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)

    # ------------------------------------------------------------------ 查询
    def items(self) -> List[InboxItem]:
        self._load()
        return list(self._items)

    def find(self, ref: str) -> Optional[InboxItem]:
        """按 ``#序号`` / 文件名 / 前缀 / sha 查找。"""

        self._load()
        text = str(ref or "").strip()
        if not text:
            return None
        if text.startswith("#") and text[1:].isdigit():
            index = int(text[1:])
            if 1 <= index <= len(self._items):
                return self._items[-index]
            return None
        lowered = text.lower()
        for item in reversed(self._items):
            if item.name.lower() == lowered or item.stored_name.lower() == lowered:
                return item
        for item in reversed(self._items):
            if item.sha256.startswith(lowered) or lowered in item.name.lower():
                return item
        return None

    def path_of(self, item: InboxItem) -> Path:
        return self.root / item.rel_path

    def exists(self, item: InboxItem) -> bool:
        return self.path_of(item).is_file()

    # ------------------------------------------------------------------ 写入
    def add_path(
        self,
        source_path: Path,
        *,
        name: str = "",
        source: str = "chat",
        sender_id: str = "",
        sender_name: str = "",
        session_id: str = "",
        chat_type: str = "",
        message_id: str = "",
        origin_url: str = "",
        note: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """把本地已有文件收进收件箱（按内容哈希去重）。"""

        self._load()
        source_path = Path(source_path)
        if not source_path.is_file():
            raise FileNotFoundError(f"文件不存在：{source_path}")
        size = source_path.stat().st_size
        if size > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"文件超过 {human_size(MAX_DOWNLOAD_BYTES)} 上限：{human_size(size)}")

        digest = hashlib.sha256()
        with source_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        sha = digest.hexdigest()

        existing = next((item for item in self._items if item.sha256 == sha), None)
        if existing is not None:
            existing.seen += 1
            existing.created_at = time.time()
            if message_id:
                existing.message_id = message_id
            self._save()
            return {"item": existing.to_dict(), "deduped": True}

        display_name = safe_filename(name or source_path.name)
        day = time.strftime("%Y%m%d")
        folder = self.root / day
        folder.mkdir(parents=True, exist_ok=True)
        stored_name = f"{sha[:8]}_{display_name}"
        target = folder / stored_name
        if target.exists():
            target = folder / f"{sha[:8]}_{int(time.time())}_{display_name}"

        if source_path.resolve() != target.resolve():
            shutil.copy2(source_path, target)

        item = InboxItem(
            item_id=sha[:16],
            name=display_name,
            stored_name=target.name,
            rel_path=str(target.relative_to(self.root)).replace("\\", "/"),
            size=target.stat().st_size,
            sha256=sha,
            mime_type=_guess_mime(display_name),
            source=source,
            sender_id=str(sender_id or ""),
            sender_name=str(sender_name or ""),
            session_id=str(session_id or ""),
            chat_type=str(chat_type or ""),
            message_id=str(message_id or ""),
            origin_url=str(origin_url or ""),
            created_at=time.time(),
            seen=1,
            note=note,
            extra=dict(extra or {}),
        )
        self._items.append(item)
        self._save()
        return {"item": item.to_dict(), "deduped": False}

    def add_bytes(
        self,
        data: bytes,
        name: str,
        *,
        source: str = "chat",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """把内存里的字节收进收件箱。"""

        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix="office-inbox-"))
        try:
            tmp_path = tmp_dir / safe_filename(name)
            tmp_path.write_bytes(data)
            return self.add_path(tmp_path, name=name, source=source, **kwargs)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ 下载
    def download(
        self,
        url: str,
        name: str = "",
        *,
        proxy: str = "",
        timeout: float = 20.0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """下载远端文件并入库。``file://`` 直接当本地路径处理。"""

        url = str(url or "").strip()
        if not url:
            raise ValueError("缺少下载链接")
        if url.startswith("file://"):
            local = Path(urllib.request.url2pathname(urllib.parse.urlparse(url).path))
            return self.add_path(local, name=name or local.name, **kwargs)

        if url.startswith("//"):
            url = "https:" + url

        request = urllib.request.Request(url, headers={"User-Agent": "MaiBot-OfficeToolkit/1.1"})
        handlers: List[Any] = []
        if proxy and url.lower().startswith(("http://", "https://")):
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        opener = urllib.request.build_opener(*handlers)

        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix="office-dl-"))
        try:
            tmp_path = tmp_dir / safe_filename(name or "download.bin")
            total = 0
            with opener.open(request, timeout=timeout) as response, tmp_path.open("wb") as out:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError(f"文件超过 {human_size(MAX_DOWNLOAD_BYTES)} 上限")
                    out.write(chunk)
            resolved_name = name
            if not resolved_name:
                disposition = ""
                try:
                    disposition = response.headers.get("Content-Disposition") or ""
                except Exception:  # pragma: no cover - 响应已关闭时忽略
                    disposition = ""
                match = re.search(r"filename\*=UTF-8''([^;]+)", disposition or "")
                if match:
                    resolved_name = urllib.parse.unquote(match.group(1))
                elif disposition:
                    match = re.search(r'filename="?([^";]+)"?', disposition)
                    if match:
                        resolved_name = match.group(1)
            if not resolved_name:
                path_name = Path(urllib.parse.urlparse(url).path).name
                resolved_name = path_name or "download.bin"
            return self.add_path(tmp_path, name=resolved_name, origin_url=url, **kwargs)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ 统计
    def summary(self) -> Dict[str, Any]:
        items = self.items()
        total = sum(item.size for item in items)
        return {"count": len(items), "total_size": total, "total_text": human_size(total)}


def extract_file_segments(message: Any) -> List[Dict[str, str]]:
    """从消息字典里提取文件段（兼容结构化段与适配器转成的文本段）。"""

    results: List[Dict[str, str]] = []
    if not isinstance(message, dict):
        return results

    raw = message.get("raw_message") or message.get("message") or []
    segments: Iterable[Any]
    if isinstance(raw, dict):
        segments = raw.get("components") or raw.get("segments") or []
    elif isinstance(raw, list):
        segments = raw
    else:
        segments = []

    for segment in segments:
        if not isinstance(segment, dict):
            continue
        seg_type = str(segment.get("type") or "").lower()
        data = segment.get("data") if isinstance(segment.get("data"), dict) else segment
        if seg_type in {"file", "document"}:
            name = str(data.get("name") or data.get("file") or data.get("file_name") or "未命名文件")
            results.append(
                {
                    "name": name,
                    "url": str(data.get("url") or data.get("file_url") or ""),
                    "size": str(data.get("size") or data.get("file_size") or ""),
                    "file_id": str(data.get("file_id") or data.get("id") or ""),
                }
            )
        elif seg_type in {"text", "plain"}:
            results.extend(parse_file_text(str(data.get("text") or "")))

    for key in ("processed_plain_text", "plain_text"):
        text = message.get(key)
        if isinstance(text, str) and "[文件]" in text:
            for entry in parse_file_text(text):
                if not any(item.get("url") and item.get("url") == entry.get("url") for item in results):
                    results.append(entry)
    return results
