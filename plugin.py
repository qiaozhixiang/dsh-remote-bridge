"""办公文件工具箱插件。

把 Word / Excel / PowerPoint / ZIP 的读取、生成与投递接进 MaiBot：

* QQ 命令：``/office`` 系列（读取、生成、打包、解压、发送）
* WebUI：插件扩展页里的文件工作台（列表 / 预览 / 生成 / 发送）
* 与 dsh-remote-bridge 联动：把 DSH 会话里生成的文件直接取过来发送

全部实现只依赖 Python 标准库，不 import 任何 ``src.*``，复制到任意
MaiBot 1.3.x 的 ``plugins/`` 目录即可使用。
"""

from __future__ import annotations

import asyncio
import importlib
import re
import shutil
import importlib.util
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from maibot_sdk import API, EventHandler, Field, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import EventType

_MODULE_PREFIX = "maibot_community_office_toolkit"


def _load_sibling(name: str) -> Any:
    """加载插件同目录下的模块。

    MaiBot 用 ``spec_from_file_location`` 加载 ``plugin.py``，插件目录本身不在
    ``sys.path`` 上，直接 ``import office_io`` 会 ModuleNotFoundError。
    """

    module_name = f"{_MODULE_PREFIX}.{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError:
        pass
    path = Path(__file__).resolve().with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 兜底
        raise ImportError(f"无法加载插件同目录模块: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


_office_io = _load_sibling("office_io")
_files = _load_sibling("files")
_commands = _load_sibling("commands")
_commands_extra = _load_sibling("commands_extra")
_inbox = _load_sibling("inbox")
_analysis = _load_sibling("analysis")
_ops = _load_sibling("ops")
_agent = _load_sibling("agent")

OfficeError = _office_io.OfficeError
read_any = _office_io.read_any
preview_text = _office_io.preview
docx_from_markdown = _office_io.docx_from_markdown
create_docx = _office_io.create_docx
xlsx_from_csv = _office_io.xlsx_from_csv
create_xlsx = _office_io.create_xlsx
pptx_from_outline = _office_io.pptx_from_outline
create_pptx = _office_io.create_pptx
create_zip = _office_io.create_zip
extract_zip = _office_io.extract_zip
describe_file = _office_io.describe
human_size = _office_io.human_size
kind_of = _office_io.kind_of
kind_label = _office_io.kind_label

PathError = _files.PathError
resolve_user_path = _files.resolve_user_path
file_meta = _files.file_meta
to_file_payload = _files.to_file_payload
sanitize_name = _files.sanitize_name
unique_path = _files.unique_path
parse_index = _files.parse_index
list_dir = _files.list_dir

OfficeCommandMixin = _commands.OfficeCommandMixin
OfficeExtraCommandMixin = _commands_extra.OfficeExtraCommandMixin
InboxStore = _inbox.InboxStore
extract_file_segments = _inbox.extract_file_segments
analysis = _analysis
ops_engine = _ops
agent = _agent


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _clean_path_text(value: Any) -> str:
    """把用户填的路径清理干净。

    WebUI 的输入框里经常有人连引号一起填（``"E:\\MaiBot\\workspace"``），存进 TOML 后
    引号会变成路径的一部分，Windows 直接报 WinError 123。这里统一剥掉成对引号。
    """

    text = str(value or "").strip()
    pairs = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"), ("「", "」"))
    changed = True
    while changed and len(text) >= 2:
        changed = False
        for left, right in pairs:
            if text.startswith(left) and text.endswith(right):
                text = text[1:-1].strip()
                changed = True
                break
    return text


def _looks_like_path(value: str) -> bool:
    """粗筛一下路径是否可用（Windows 下这些字符不允许出现在路径里）。"""

    if not value:
        return False
    return not any(char in value for char in '<>"|?*')


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.2.2", description="配置版本")


class StorageConfig(PluginConfigBase):
    """文件存储配置。"""

    __ui_label__ = "存储"
    __ui_icon__ = "folder"
    __ui_order__ = 1

    workspace: str = Field(
        default="",
        description="生成的文档存放目录；留空使用插件数据目录下的 files",
    )
    allowed_roots: List[str] = Field(
        default_factory=list,
        description="允许读取/发送的目录白名单；留空表示不限制（敏感目录始终排除）",
    )
    max_size_mb: int = Field(default=50, description="单个文件投递大小上限（MB）")


class DocumentsConfig(PluginConfigBase):
    """文档处理配置。"""

    __ui_label__ = "文档"
    __ui_icon__ = "file-text"
    __ui_order__ = 2

    preview_chars: int = Field(default=1500, description="命令预览时的最大字符数")
    max_rows: int = Field(default=500, description="表格读取的最大行数")
    max_paragraphs: int = Field(default=800, description="文档读取的最大段落数")


class WhitelistConfig(PluginConfigBase):
    """QQ 白名单配置。"""

    __ui_label__ = "QQ 白名单"
    __ui_icon__ = "users"
    __ui_order__ = 3

    enabled: bool = Field(default=True, description="是否启用 QQ 白名单校验")
    qq_numbers: List[str] = Field(default_factory=list, description="允许使用 /office 命令的 QQ 号")
    admins: List[str] = Field(default_factory=list, description="管理员 QQ 号")
    ignore_others: bool = Field(default=True, description="非白名单用户静默忽略（不回复、不进模型）")
    max_reply_chars: int = Field(default=1500, description="单条消息最大字符数，超出自动分段")


class IntegrationConfig(PluginConfigBase):
    """与其他插件联动。"""

    __ui_label__ = "联动"
    __ui_icon__ = "link"
    __ui_order__ = 4

    dsh_enabled: bool = Field(default=True, description="是否启用与 dsh-remote-bridge 的联动")
    dsh_plugin_id: str = Field(
        default="github.qiaozhixiang.dsh-remote-bridge",
        description="DSH 桥接插件的插件 ID",
    )


class InboxConfig(PluginConfigBase):
    """聊天文件收件箱配置。"""

    __ui_label__ = "收件箱"
    __ui_icon__ = "inbox"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="是否把群里/私聊收到的文件自动存到本地收件箱")
    auto_capture: bool = Field(default=True, description="收到文件消息时自动入库（关闭后只能手动 /office import）")
    max_items: int = Field(default=2000, description="收件箱最多保留多少个文件记录")
    max_file_mb: int = Field(default=50, description="单个文件入库大小上限（MB）")
    download_proxy: str = Field(default="", description="下载 QQ 文件用的 HTTP 代理，例如 http://127.0.0.1:1080；留空直连")
    auto_import: bool = Field(
        default=False,
        description="收到文件时顺便复制一份到工作区（默认只在收件箱，用 /office get 取）",
    )


class AiConfig(PluginConfigBase):
    """自然语言指令配置。"""

    __ui_label__ = "智能指令"
    __ui_icon__ = "sparkles"
    __ui_order__ = 6

    enabled: bool = Field(default=True, description="是否启用 /office ai 自然语言指令")
    model: str = Field(default="", description="指定模型名；留空用 MaiBot 默认对话模型")
    temperature: float = Field(default=0.2, description="生成操作计划时的温度")
    max_steps: int = Field(default=12, description="单个指令最多允许的操作步骤数")
    allow_delete: bool = Field(default=False, description="是否允许 AI 计划里包含删除文件的操作")
    dry_run: bool = Field(default=False, description="只显示解析出的操作计划，不真正执行")
    llm_timeout_seconds: float = Field(
        default=120.0,
        description="调用宿主大模型的最长等待秒数（复杂指令建议 90~180）",
    )


class OfficeToolkitConfig(PluginConfigBase):
    """办公文件工具箱整体配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    documents: DocumentsConfig = Field(default_factory=DocumentsConfig)
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)
    integration: IntegrationConfig = Field(default_factory=IntegrationConfig)
    inbox: InboxConfig = Field(default_factory=InboxConfig)
    ai: AiConfig = Field(default_factory=AiConfig)


# ---------------------------------------------------------------------------
# 插件
# ---------------------------------------------------------------------------


def _normalize_csv_text(text: str) -> str:
    """允许用「;」当换行：QQ 单条消息里换行不方便输入。"""

    raw = str(text or "")
    if "\n" in raw:
        return raw
    return raw.replace("；", ";").replace(";", "\n")


class OfficeToolkitPlugin(OfficeCommandMixin, OfficeExtraCommandMixin, MaiBotPlugin):
    """办公文件工具箱。"""

    config_model = OfficeToolkitConfig

    def __init__(self) -> None:
        super().__init__()
        self._office_list_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._office_extra_allow: List[str] = []
        self._office_last_stream: str = ""
        self._tasks: List[asyncio.Task] = []
        self._inbox: Optional[InboxStore] = None
        self._ai_jobs: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------- 生命周期

    async def on_load(self) -> None:
        workspace = self._ensure_workspace()
        self.ctx.logger.info(f"办公文件工具箱已加载，工作区 {workspace}")

    async def on_unload(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self.ctx.logger.info("办公文件工具箱已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        del config_data, version
        if scope != "self":
            return
        workspace = self._ensure_workspace()
        self.ctx.logger.info(f"办公文件工具箱配置已更新，工作区 {workspace}")

    # ----------------------------------------------------------- 消息事件

    @EventHandler("capture_files", description="把聊天里收到的文件存到本地收件箱", event_type=EventType.ON_MESSAGE)
    async def handle_capture_files(self, message: Any = None, stream_id: str = "", **kwargs: Any):
        """收到文件消息时自动入库，供后续 /office 命令操作。"""

        del kwargs
        cfg = self._plugin_config().inbox
        if not cfg.enabled or not cfg.auto_capture or not isinstance(message, dict):
            return True, True, None, None, None
        try:
            segments = extract_file_segments(message)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug(f"解析文件消息失败：{exc}")
            return True, True, None, None, None
        if not segments:
            return True, True, None, None, None

        session_id = str(stream_id or message.get("session_id") or "")
        if session_id:
            self._office_last_stream = session_id
        sender_id = str(message.get("user_id") or message.get("sender_id") or "")
        sender_name = str(message.get("user_name") or message.get("sender_name") or "")
        message_id = str(message.get("message_id") or "")
        limit = max(1, int(cfg.max_file_mb or 50)) * 1024 * 1024
        saved = skipped = 0
        for segment in segments:
            url = str(segment.get("url") or "").strip()
            if not url:
                continue
            hint = str(segment.get("size") or "").strip()
            if hint.isdigit() and int(hint) > limit:
                skipped += 1
                continue
            try:
                self._inbox_store().download(
                    url,
                    name=str(segment.get("name") or ""),
                    proxy=str(cfg.download_proxy or ""),
                    sender_id=sender_id,
                    sender_name=sender_name,
                    session_id=session_id,
                    message_id=message_id,
                    source="chat",
                )
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning(f"收件箱入库失败（{segment.get('name')}）：{exc}")
                skipped += 1
                continue
            saved += 1
        if saved or skipped:
            self.ctx.logger.info(f"收件箱：新增 {saved} 个，跳过 {skipped} 个（会话 {session_id}）")
        return True, True, None, None, None

    # --------------------------------------------------------------- 内部

    def _plugin_config(self) -> OfficeToolkitConfig:
        raw = self.config
        if isinstance(raw, OfficeToolkitConfig):
            return raw
        return OfficeToolkitConfig()

    def _storage_config(self) -> StorageConfig:
        return self._plugin_config().storage

    def _workspace(self) -> Path:
        configured = _clean_path_text(self._storage_config().workspace)
        if configured and _looks_like_path(configured):
            path = Path(configured).expanduser()
            if not path.is_absolute():
                path = Path(self.ctx.paths.data_dir) / path
            return path
        if configured:
            self.ctx.logger.warning(f"工作区路径不合法，已回退默认目录：{configured!r}")
        return Path(self.ctx.paths.data_dir) / "files"

    def _ensure_workspace(self) -> Path:
        """确保工作区可用；配置的路径建不出来时自动退回插件数据目录。"""

        workspace = self._workspace()
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            return workspace
        except OSError as exc:
            fallback = Path(self.ctx.paths.data_dir) / "files"
            self.ctx.logger.warning(f"工作区 {workspace} 无法创建（{exc}），已改用 {fallback}")
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def _inbox_store(self) -> InboxStore:
        """收件箱存储（惰性创建，目录在插件数据目录下）。"""

        cfg = self._plugin_config().inbox
        root = Path(self.ctx.paths.data_dir) / "inbox"
        if self._inbox is None or Path(self._inbox.root) != root or self._inbox.max_items != max(50, int(cfg.max_items or 2000)):
            self._inbox = InboxStore(root, max_items=max(50, int(cfg.max_items or 2000)))
        return self._inbox

    def _allowed_roots(self) -> List[str]:
        roots = [
            _clean_path_text(item)
            for item in (self._storage_config().allowed_roots or [])
            if _clean_path_text(item)
        ]
        if not roots:
            return []
        return roots

    def _list_workspace(self, keyword: str = "") -> List[Dict[str, Any]]:
        return list_dir(self._workspace(), keyword=keyword)

    def _resolve_token(self, user_id: str, token: str) -> Path:
        """把 ``#3`` 或路径解析成 Path。"""

        text = (token or "").strip()
        if not text:
            raise PathError("请给出文件路径或 #序号")
        index = parse_index(text)
        if index is not None:
            cached = self._office_list_cache.get(user_id) or []
            if index > len(cached):
                raise PathError(f"序号超出范围：1~{len(cached)}，先用 /office ls 看看")
            return Path(str(cached[index - 1]["path"]))
        path: Optional[Path] = None
        error: Optional[PathError] = None
        try:
            path = resolve_user_path(text, workspace=self._workspace(), allowed_roots=self._allowed_roots())
        except PathError as exc:
            error = exc
        if path is not None and path.exists():
            return path
        item = self._match_chat_item(text)
        if item is not None:
            return self._copy_inbox_item(item)
        if path is not None:
            return path
        if error is not None:
            raise error
        raise PathError(f"找不到文件：{text}")

    def _new_workspace_path(self, name: str) -> Path:
        safe = sanitize_name(name)
        return unique_path(self._workspace(), safe)

    def _read_file(self, path: Path) -> Dict[str, Any]:
        max_chars = max(200, int(self._plugin_config().documents.preview_chars or 1500)) * 20
        return read_any(path, max_chars=max_chars)

    def _preview_text(self, result: Dict[str, Any], limit: int) -> str:
        return preview_text(result, limit)

    def _create_docx(self, path: Path, markdown: str) -> Dict[str, Any]:
        return docx_from_markdown(path, markdown)

    def _create_xlsx(self, path: Path, csv_text: str) -> Dict[str, Any]:
        return xlsx_from_csv(path, _normalize_csv_text(csv_text))

    def _create_pptx(self, path: Path, outline: str) -> Dict[str, Any]:
        return pptx_from_outline(path, path.stem, outline)

    def _create_zip(self, path: Path, paths: List[Path]) -> Dict[str, Any]:
        return create_zip(path, paths)

    def _extract_zip(self, path: Path, dest: Path) -> Dict[str, Any]:
        return extract_zip(path, dest)

    async def _deliver_file(self, stream_id: str, path: Path) -> bool:
        """把文件作为 ``file`` 消息段发到对话；不支持时退回文本路径。"""

        if not stream_id:
            return False
        if not path.exists() or not path.is_file():
            await self._office_say(stream_id, f"文件不存在：{path}")
            return False
        meta = file_meta(path)
        limit = max(1, int(self._storage_config().max_size_mb or 50)) * 1024 * 1024
        if int(meta.get("size") or 0) > limit:
            await self._office_say(stream_id, f"文件太大（{meta['size_text']}），超过 {self._storage_config().max_size_mb} MB 上限。")
            return False
        payload = to_file_payload(meta)
        try:
            ok = bool(await self.ctx.send.custom("file", payload, stream_id))
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning(f"发送文件失败（{path.name}）: {exc}")
            ok = False
        if not ok:
            try:
                await self.ctx.send.text(f"{path.name}：{path}", stream_id)
                return True
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning(f"退回文本发送也失败: {exc}")
                return False
        return True

    async def _dsh_files(self) -> List[Dict[str, Any]]:
        """向 dsh-remote-bridge 要「当前会话最近生成的文件」。"""

        cfg = self._plugin_config().integration
        if not cfg.dsh_enabled:
            return []
        plugin_id = (cfg.dsh_plugin_id or "").strip()
        if not plugin_id:
            return []
        try:
            data = await self.ctx.api.call(f"{plugin_id}.current_session", user_id="")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug(f"调用 {plugin_id}.current_session 失败: {exc}")
            return []
        if not isinstance(data, dict):
            return []
        files = data.get("files")
        if not isinstance(files, list):
            return []
        return [item for item in files if isinstance(item, dict) and item.get("path")]

    # ------------------------------------------------------------ 聊天文件

    def _inbox_config(self) -> Any:
        return self._plugin_config().inbox

    def _inbox_items(self) -> List[Any]:
        try:
            return self._inbox_store().items()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug(f"读取收件箱失败：{exc}")
            return []

    def _copy_inbox_item(self, item: Any) -> Path:
        """把收件箱里的文件复制进工作区，返回工作区路径。"""

        store = self._inbox_store()
        source = store.path_of(item)
        target = unique_path(self._workspace(), sanitize_name(getattr(item, "name", "") or source.name))
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)
        return target

    async def pull_chat_files(self, stream_id: str, message: Optional[Dict[str, Any]] = None,
                              *, limit: int = 25) -> List[str]:
        """把当前会话最近消息（含被回复的那条）里的文件收进收件箱。"""

        cfg = self._inbox_config()
        if not getattr(cfg, "enabled", True) or not stream_id:
            return []
        store = self._inbox_store()
        proxy = str(getattr(cfg, "download_proxy", "") or "")
        messages: List[Dict[str, Any]] = []
        current = message if isinstance(message, dict) else {}
        reply_id = str(current.get("reply_to") or "")
        if reply_id:
            try:
                replied = await self.ctx.message.get_by_id(reply_id, stream_id=stream_id)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.debug(f"读取被回复消息失败：{exc}")
                replied = None
            if isinstance(replied, dict):
                messages.append(replied)
        if current:
            messages.append(current)
        try:
            recent = await self.ctx.message.get_recent(chat_id=stream_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug(f"读取最近消息失败：{exc}")
            recent = None
        if isinstance(recent, list):
            messages.extend(item for item in recent if isinstance(item, dict))

        names: List[str] = []
        seen = set()
        for msg in messages:
            info = msg.get("message_info") if isinstance(msg.get("message_info"), dict) else {}
            for segment in _inbox.extract_file_segments(msg):
                url = str(segment.get("url") or "")
                name = str(segment.get("name") or "")
                key = (name, url)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    store.download(
                        url, name, proxy=proxy, timeout=25,
                        source="chat", session_id=stream_id,
                        message_id=str(msg.get("message_id") or ""),
                        sender_id=str(info.get("user_id") or ""),
                        sender_name=str(info.get("user_name") or info.get("user_nickname") or ""),
                    )
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.debug(f"下载聊天文件失败（{name}）：{exc}")
                    continue
                if name:
                    names.append(name)
        if names:
            self.ctx.logger.info(f"从当前会话收取文件：{', '.join(names[:5])}")
        return names

    def _match_chat_item(self, ref: str) -> Optional[Any]:
        text = str(ref or "").strip()
        if not text:
            return None
        for item in reversed(self._inbox_items()):
            name = str(getattr(item, "name", "") or "")
            if not name:
                continue
            if name.lower() == text.lower() or Path(name).stem.lower() == Path(text).stem.lower():
                return item
        try:
            return self._inbox_store().find(text)
        except Exception:  # noqa: BLE001
            return None

    def _prepare_chat_refs(self, text: str) -> str:
        """把指令里提到的聊天文件、「上面的表格」落到工作区，并补上真实文件名。"""

        instruction = str(text or "")
        items = self._inbox_items()
        if not items:
            return instruction
        lowered = instruction.lower()
        picked = None
        for item in reversed(items):
            name = str(getattr(item, "name", "") or "")
            if not name:
                continue
            if name.lower() in lowered or Path(name).stem.lower() in lowered:
                picked = item
                break
        if picked is None and re.search(r"上面|刚才|刚刚|上一条|上一个|这个文件|那个文件|附件|发的|发来|收到", instruction):
            picked = items[-1]
        if picked is None:
            return instruction
        try:
            self._copy_inbox_item(picked)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug(f"导入聊天文件失败：{exc}")
            return instruction
        name = str(getattr(picked, "name", "") or "")
        if name and name.lower() not in lowered:
            return f"{instruction}（使用文件：{name}）"
        return instruction

    # ------------------------------------------------------------ 智能指令

    def _ai_config(self) -> Any:
        return self._plugin_config().ai

    async def _llm_generate(self, prompt: str) -> Dict[str, Any]:
        """用更长的超时调用宿主 LLM（默认 cap.call 只有 30 秒，复杂指令会超时）。"""

        cfg = self._ai_config()
        timeout = float(getattr(cfg, "llm_timeout_seconds", 120.0) or 120.0)
        result = await self.ctx.call_capability(
            "llm.generate",
            timeout_ms=int(timeout * 1000),
            prompt=prompt,
            model=str(getattr(cfg, "model", "") or ""),
            temperature=float(getattr(cfg, "temperature", 0.2) or 0.2),
            max_tokens=1200,
        )
        return result if isinstance(result, dict) else {"success": False, "reason": "模型返回格式异常"}

    def _ops_runtime(self) -> Any:
        """给 ops 引擎提供「解析路径 / 新建路径」两个能力。"""

        def resolve(token: str) -> Path:
            text = (token or "").strip()
            if not text:
                raise PathError("请给出文件路径")
            return resolve_user_path(text, workspace=self._workspace(), allowed_roots=self._allowed_roots())

        return ops_engine.Runtime(resolve=resolve, new_path=self._new_workspace_path, workspace=self._workspace())

    def _file_hints(self, limit: int = 30) -> List[Dict[str, Any]]:
        hints: List[Dict[str, Any]] = []
        for item in self._list_workspace()[:limit]:
            try:
                hints.append(agent.file_hints(Path(str(item["path"]))))
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.debug(f"读取文件摘要失败：{exc}")
        return hints

    async def run_instruction(self, instruction: str, *, stream_id: str = "", dry_run: bool = False,
                             message: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """把一句自然语言指令翻译成操作计划并执行。"""

        cfg = self._ai_config()
        text = (instruction or "").strip()
        if not text:
            return {"ok": False, "message": "请给出要做什么，例如：/office ai 把成绩单里不及格的挑出来发给我"}
        if not getattr(cfg, "enabled", True):
            return {"ok": False, "message": "智能指令已关闭（config.toml 的 [ai] enabled = false）"}

        if stream_id:
            try:
                await self.pull_chat_files(stream_id, message)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.debug(f"收取聊天文件失败：{exc}")
            text = self._prepare_chat_refs(text)

        hints = self._file_hints()
        files_text = agent.describe_files(hints)
        names = [item["name"] for item in hints]
        headers = {item.get("name"): item.get("header") or [] for item in hints}

        # 先用关键词规则解析：常见指令能秒出结果，不必等模型。
        plan = agent.plan_with_rules(text, names, headers)
        source = "规则"
        why = ""
        if plan is None:
            plan, why = await agent.plan_with_llm(
                self._llm_generate,
                text,
                files_text,
                workspace=str(self._workspace()),
                model=str(getattr(cfg, "model", "") or ""),
                temperature=float(getattr(cfg, "temperature", 0.2) or 0.2),
                max_steps=int(getattr(cfg, "max_steps", 12) or 12),
            )
            source = "模型"
        if plan is None:
            return {
                "ok": False,
                "message": f"没能理解这条指令（{why or '没有匹配到可用操作'}）。\n{ops_engine.catalog_text()}",
            }

        summary = agent.summarize_plan(plan)
        if dry_run or bool(getattr(cfg, "dry_run", False)):
            return {"ok": True, "dry_run": True, "source": source, "plan": plan, "summary": summary}

        result = ops_engine.run_plan(
            plan,
            self._ops_runtime(),
            max_steps=int(getattr(cfg, "max_steps", 12) or 12),
            allow_delete=bool(getattr(cfg, "allow_delete", False)),
        )
        delivered: List[str] = []
        for item in result.sends:
            path = Path(item)
            if await self._deliver_file(stream_id, path):
                delivered.append(path.name)
        previews = list(result.previews)
        if result.ok and not previews and result.outputs:
            last = result.outputs[-1]
            try:
                previews.append(ops_engine.read_text_any(Path(str(last.get("path") or "")), 1200))
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.debug(f"补充预览失败：{exc}")
        return {
            "ok": bool(result.ok),
            "source": source,
            "plan": plan,
            "summary": summary,
            "logs": result.logs,
            "previews": previews,
            "outputs": result.outputs,
            "sent": delivered,
            "error": result.error,
        }

    @API("office_ai", description="用一句自然语言操作文件（自动生成并执行操作计划）", version="1")
    async def office_ai(
        self,
        instruction: str,
        dry_run: bool = False,
        stream_id: str = "",
        wait_seconds: float = 6.0,
    ) -> Dict[str, Any]:
        text = str(instruction or "").strip()
        if not text:
            return {"ok": False, "message": "请给出要做什么，例如：把成绩单里不及格的挑出来发给我"}
        job_id = self._start_ai_job(text, stream_id=stream_id, dry_run=bool(dry_run))
        # WebUI 的宿主调用有 10 秒上限，这里最多等 8 秒，超时就转成异步任务
        try:
            budget = float(wait_seconds)
        except (TypeError, ValueError):
            budget = 6.0
        deadline = time.time() + max(0.0, min(budget, 8.0))
        while time.time() < deadline:
            job = self._ai_jobs.get(job_id) or {}
            if job.get("state") != "running":
                result = dict(job.get("result") or {})
                result.setdefault("job_id", job_id)
                return result
            await asyncio.sleep(0.2)
        return {
            "ok": True,
            "pending": True,
            "job_id": job_id,
            "message": "指令已提交，正在后台执行；稍候自动刷新结果。",
        }

    def _start_ai_job(self, instruction: str, *, stream_id: str = "", dry_run: bool = False) -> str:
        job_id = uuid.uuid4().hex[:12]
        self._ai_jobs[job_id] = {
            "job_id": job_id,
            "state": "running",
            "instruction": instruction,
            "started_at": time.time(),
            "result": None,
        }
        while len(self._ai_jobs) > 20:
            self._ai_jobs.pop(next(iter(self._ai_jobs)), None)

        async def _run() -> None:
            job = self._ai_jobs.get(job_id)
            if job is None:
                return
            try:
                job["result"] = await self.run_instruction(instruction, stream_id=stream_id, dry_run=dry_run)
                job["state"] = "done"
            except asyncio.CancelledError:
                job["state"] = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.exception(f"智能指令执行失败：{exc}")
                job["result"] = {"ok": False, "message": f"执行失败：{exc}"}
                job["state"] = "failed"

        task = asyncio.create_task(_run())
        self._tasks.append(task)

        def _forget(done: asyncio.Task) -> None:
            if done in self._tasks:
                self._tasks.remove(done)

        task.add_done_callback(_forget)
        return job_id

    @API("ai_status", description="查询智能指令的后台执行状态", version="1")
    async def ai_status(self, job_id: str) -> Dict[str, Any]:
        job = self._ai_jobs.get(str(job_id or ""))
        if not job:
            return {"ok": False, "state": "missing", "message": "任务不存在或已过期"}
        data: Dict[str, Any] = {
            "ok": True,
            "job_id": job["job_id"],
            "state": job["state"],
            "instruction": job.get("instruction", ""),
        }
        if job.get("state") != "running" and job.get("result") is not None:
            data["result"] = job["result"]
        return data

    @API("ops_catalog", description="列出智能指令支持的操作", version="1")
    async def ops_catalog(self) -> Dict[str, Any]:
        return {"ok": True, "text": ops_engine.catalog_text()}

    # ---------------------------------------------------------------- API

    @API("toolkit_status", description="返回办公文件工具箱的状态", version="1")
    async def toolkit_status(self) -> Dict[str, Any]:
        workspace = self._workspace()
        items = self._list_workspace()
        return {
            "ok": True,
            "workspace": str(workspace),
            "files": len(items),
            "dsh_plugin": self._plugin_config().integration.dsh_plugin_id,
            "dsh_enabled": bool(self._plugin_config().integration.dsh_enabled),
            "kinds": ["docx", "xlsx", "pptx", "pdf", "csv", "txt", "zip"],
        }

    @API("list_files", description="列出工作区文件", version="1")
    async def list_files(self, keyword: str = "") -> Dict[str, Any]:
        items = self._list_workspace(str(keyword or ""))
        return {"ok": True, "items": items, "count": len(items), "workspace": str(self._workspace())}

    @API("read_file", description="读取并预览一个文件", version="1")
    async def read_file(self, path: str, limit: int = 0) -> Dict[str, Any]:
        target = resolve_user_path(path, workspace=self._workspace(), allowed_roots=self._allowed_roots())
        result = self._read_file(target)
        chars = int(limit or 0) or max(300, int(self._plugin_config().documents.preview_chars or 1500))
        return {
            "ok": True,
            "kind": result.get("kind"),
            "kind_label": result.get("kind_label"),
            "name": result.get("name"),
            "text": result.get("text") or "",
            "preview": self._preview_text(result, chars),
            "meta": result.get("meta") or {},
            "size": result.get("size"),
        }

    @API("create_document", description="生成 Word / Excel / PPT 文档", version="1")
    async def create_document(self, kind: str, name: str, content: str = "", title: str = "") -> Dict[str, Any]:
        kind_text = str(kind or "").strip().lower()
        safe_name = sanitize_name(name or "未命名")
        if kind_text == "docx":
            path = self._new_workspace_path(safe_name if safe_name.lower().endswith(".docx") else f"{safe_name}.docx")
            markdown = str(content or "")
            if title:
                markdown = f"# {title}\n{markdown}"
            info = self._create_docx(path, markdown)
        elif kind_text == "xlsx":
            path = self._new_workspace_path(safe_name if safe_name.lower().endswith(".xlsx") else f"{safe_name}.xlsx")
            info = self._create_xlsx(path, str(content or ""))
        elif kind_text == "pptx":
            path = self._new_workspace_path(safe_name if safe_name.lower().endswith(".pptx") else f"{safe_name}.pptx")
            info = self._create_pptx(path, str(content or ""))
        else:
            raise ValueError("kind 只能是 docx / xlsx / pptx")
        return {"ok": True, "kind": info.get("kind"), "name": path.name, "path": str(path), "size": info.get("size")}

    @API("zip_files", description="把若干文件打包成 ZIP", version="1")
    async def zip_files(self, name: str, paths: str) -> Dict[str, Any]:
        tokens = [token for token in str(paths or "").replace("，", " ").replace(",", " ").split() if token]
        if not tokens:
            raise ValueError("请给出要压缩的文件路径")
        sources = [
            resolve_user_path(token, workspace=self._workspace(), allowed_roots=self._allowed_roots())
            for token in tokens
        ]
        safe_name = sanitize_name(name or "打包")
        target = self._new_workspace_path(safe_name if safe_name.lower().endswith(".zip") else f"{safe_name}.zip")
        info = create_zip(target, sources)
        return {"ok": True, "name": target.name, "path": str(target), "entries": info.get("entries"), "size": info.get("size")}

    @API("unzip_file", description="解压 ZIP 到工作区", version="1")
    async def unzip_file(self, path: str, dest: str = "") -> Dict[str, Any]:
        source = resolve_user_path(path, workspace=self._workspace(), allowed_roots=self._allowed_roots())
        target = Path(dest).expanduser() if str(dest or "").strip() else self._workspace() / f"{source.stem}_解压"
        if not target.is_absolute():
            target = self._workspace() / target
        info = extract_zip(source, target)
        return {"ok": True, "dest": info.get("dest"), "entries": info.get("entries")}

    @API("send_file", description="把文件发到指定对话", version="1")
    async def send_file(self, path: str, stream_id: str = "") -> Dict[str, Any]:
        target = resolve_user_path(path, workspace=self._workspace(), allowed_roots=self._allowed_roots())
        target_stream = str(stream_id or "").strip() or str(getattr(self, "_office_last_stream", "") or "")
        if not target_stream:
            return {
                "ok": False,
                "name": target.name,
                "path": str(target),
                "message": "还没有可用的对话：先在 QQ 里用一次 /office 命令，页面就知道该发到哪里了",
            }
        sent = await self._deliver_file(target_stream, target)
        return {"ok": sent, "name": target.name, "path": str(target), "size": target.stat().st_size}

    @API("delete_file", description="删除工作区里的文件", version="1")
    async def delete_file(self, path: str) -> Dict[str, Any]:
        target = resolve_user_path(path, workspace=self._workspace(), allowed_roots=self._allowed_roots())
        workspace = self._workspace().resolve()
        resolved = target.resolve()
        if workspace != resolved and workspace not in resolved.parents:
            raise ValueError("只能删除工作区里的文件")
        target.unlink()
        return {"ok": True, "name": target.name}

    @API("dsh_files", description="列出 DSH 会话里生成的文件", version="1")
    async def dsh_files(self) -> Dict[str, Any]:
        items = await self._dsh_files()
        return {"ok": True, "items": items, "count": len(items)}

    @API("inbox_list", description="列出收件箱里的聊天文件", version="1")
    async def inbox_list(self, keyword: str = "") -> Dict[str, Any]:
        store = self._inbox_store()
        lowered = str(keyword or "").strip().lower()
        items = store.items()
        if lowered:
            items = [
                item
                for item in items
                if lowered in item.name.lower() or lowered in (item.sender_name or "").lower()
            ]
        recent = list(reversed(items[-100:]))
        return {
            "ok": True,
            "count": len(items),
            "summary": store.summary(),
            "items": [
                {
                    "index": index,
                    "name": item.name,
                    "size": item.size,
                    "size_text": human_size(item.size),
                    "sender": item.sender_name or item.sender_id,
                    "source": item.source,
                    "created_at": item.created_at,
                    "sha256": item.sha256[:12],
                }
                for index, item in enumerate(recent, start=1)
            ],
        }

    @API("inbox_get", description="把收件箱文件复制到工作区", version="1")
    async def inbox_get(self, ref: str) -> Dict[str, Any]:
        store = self._inbox_store()
        item = store.find(ref)
        if item is None:
            raise ValueError(f"收件箱里没有：{ref}")
        source = store.path_of(item)
        if not source.is_file():
            raise ValueError(f"文件已被清理：{item.rel_path}")
        target = self._new_workspace_path(item.name)
        target.write_bytes(source.read_bytes())
        return {"ok": True, "name": target.name, "path": str(target), "size": target.stat().st_size}

    @API("diff_files", description="对比两个文件的差异", version="1")
    async def diff_files(self, left: str, right: str) -> Dict[str, Any]:
        first = self._resolve_token("", left)
        second = self._resolve_token("", right)
        result = analysis.diff_files(first, second)
        return {"ok": True, **result}

    @API("whitelist_overview", description="返回白名单与权限概览", version="1")
    async def whitelist_overview(self) -> Dict[str, Any]:
        cfg = self._office_cfg()
        return {
            "enabled": bool(cfg.enabled),
            "whitelist": [str(item) for item in (cfg.qq_numbers or [])],
            "admins": [str(item) for item in (cfg.admins or [])],
            "ignore_others": bool(cfg.ignore_others),
            "subcommands": [
                "ls", "read", "send", "docx", "xlsx", "pptx",
                "zip", "unzip", "rm", "dsh", "whoami", "help",
                "inbox", "sync", "get", "import", "diff", "batch",
                "edit", "check", "scholar", "format",
            ],
        }


def create_plugin() -> OfficeToolkitPlugin:
    """MaiBot 插件工厂。"""

    return OfficeToolkitPlugin()
