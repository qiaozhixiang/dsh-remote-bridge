"""DSH 远程桥接插件。

把 DeepSeek Harness（DSH）的远程会话能力接进 MaiBot：

* 在 MaiBot WebUI 里内嵌一个完整的 DSH 控制台页面（自定义页面，非 iframe 外链）
* 会话列表 / 会话详情 / 历史消息 / 模型切换 / 新建 / 重命名 / 发送 / 中断
* 实时跟随：插件侧维持到 DSH 的 WebSocket 长连接，页面用游标增量拉取

插件只通过 ``maibot_sdk`` 与宿主交互，不 import 任何 ``src.*``，因此可以直接
复制到任意 MaiBot 1.3.x 实例的 ``plugins/`` 目录下使用。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import ipaddress
import json
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import urlsplit

from maibot_sdk import API, Field, MaiBotPlugin, PluginConfigBase


_MODULE_PREFIX = "maibot_community_dsh_remote_bridge"


def _is_loopback_url(url: str) -> bool:
    """判断地址是不是本机回环，回环链路不该走代理。"""

    try:
        host = (urlsplit(url).hostname or "").strip().lower()
    except Exception:  # noqa: BLE001
        return False
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _is_private_host(host: str) -> bool:
    """内网地址（回环 / RFC1918 / 链路本地 / .local / 单段主机名）。

    这些地址必须直连：走系统代理时，代理往往在另一个网段甚至另一台机器上，
    根本到不了本机的局域网地址，表现就是「连接被拒绝」。
    """

    host = (host or "").strip().lower().strip("[]")
    if not host:
        return False
    if host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
        return True
    if host.endswith((".local", ".lan", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def _proxy_for(url: str, proxy: str) -> str:
    proxy = (proxy or "").strip()
    if not proxy:
        return ""
    try:
        host = urlsplit(url).hostname or ""
    except Exception:  # noqa: BLE001
        return proxy
    if _is_loopback_url(url) or _is_private_host(host):
        return ""
    return proxy


def _is_model_unavailable(error: BaseException) -> bool:
    """判断异常是不是「会话选的模型没有适配器」。"""
    code = str(getattr(error, "code", "") or "")
    text = str(getattr(error, "message", "") or error or "")
    return (
        "model-unavailable" in code
        or "model-unavailable" in text
        or "no adapter serves provider" in text
    )


def _load_sibling(name: str) -> Any:
    """加载插件同目录下的模块。

    MaiBot 的插件加载器用 ``spec_from_file_location`` 加载 ``plugin.py``，插件目录
    本身并不在 ``sys.path`` 上，所以直接 ``import dsh_client`` 会 ModuleNotFoundError。
    这里先试常规导入，失败后按文件路径兜底，并用带前缀的模块名避免与其他插件重名。
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


_dsh_client = _load_sibling("dsh_client")
DshBridge = _dsh_client.DshBridge
DshClient = _dsh_client.DshClient
DshError = _dsh_client.DshError
normalize_base_url = _dsh_client.normalize_base_url

_tunnel_module = _load_sibling("tunnel")
TunnelManager = _tunnel_module.TunnelManager

_qq_module = _load_sibling("qq_commands")
QqCommandMixin = _qq_module.QqCommandMixin

_files_module = _load_sibling("files")
collect_files = _files_module.collect_files
to_file_payload = _files_module.to_file_payload
human_size = _files_module.human_size
file_meta = _files_module.file_meta


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.6.4", description="配置版本")


class ConnectionConfig(PluginConfigBase):
    """DSH 连接配置。"""

    __ui_label__ = "连接"
    __ui_icon__ = "plug"
    __ui_order__ = 1

    base_url: str = Field(default="http://127.0.0.1:3080", description="DSH Web 地址，例如 http://127.0.0.1:3080")
    token: str = Field(default="", description="DSH 访问令牌；loopback 直连通常留空")
    timeout_seconds: float = Field(default=12.0, description="单次 HTTP 调用超时（秒）")
    verify_tls: bool = Field(default=True, description="HTTPS 是否校验证书")
    http_proxy: str = Field(
        default="",
        description="访问公网地址时使用的 HTTP 代理，例如 http://127.0.0.1:1080；内网直连留空",
    )


class BehaviorConfig(PluginConfigBase):
    """行为配置。"""

    __ui_label__ = "行为"
    __ui_icon__ = "sliders-horizontal"
    __ui_order__ = 2

    page_size: int = Field(default=60, description="历史消息一次拉取条数")
    max_sessions: int = Field(default=300, description="会话列表最多返回条数")
    stream_buffer_size: int = Field(default=600, description="每个会话的实时帧缓冲上限")
    allow_write: bool = Field(default=True, description="是否允许发送消息、新建会话等写操作")


class WhitelistConfig(PluginConfigBase):
    """QQ 白名单配置。"""

    __ui_label__ = "QQ 白名单"
    __ui_icon__ = "users"
    __ui_order__ = 3

    enabled: bool = Field(default=True, description="是否启用 QQ 白名单校验")
    qq_numbers: List[str] = Field(
        default_factory=list,
        description="允许使用 /dsh 命令的 QQ 号列表；留空表示除管理员外全部拒绝",
    )
    admins: List[str] = Field(default_factory=list, description="管理员 QQ 号，可用 /dsh allow 管理白名单")
    ignore_others: bool = Field(
        default=True,
        description="非白名单用户的 /dsh 命令静默忽略（不回复、不进入模型）",
    )
    reply_timeout_seconds: int = Field(default=180, description="等待 DSH 回复的超时时间（秒）")
    max_reply_chars: int = Field(default=1200, description="单条 QQ 消息最大字符数，超出自动分段")


class TunnelConfig(PluginConfigBase):
    """公网隧道配置。"""

    __ui_label__ = "公网隧道"
    __ui_icon__ = "globe"
    __ui_order__ = 4

    enabled: bool = Field(default=False, description="是否允许启动公网隧道（默认关闭，仅在需要临时调试时手动打开）")
    auto_start: bool = Field(default=False, description="插件加载时自动启动隧道")
    cloudflared_path: str = Field(default="", description="cloudflared 可执行文件路径；留空自动探测")
    target_url: str = Field(default="", description="隧道转发的目标地址；留空使用 connection.base_url")
    lan_url: str = Field(
        default="",
        description="内网通道地址，例如 http://192.168.1.10:3088；留空自动读取 DSH 的 access.json 监听地址",
    )
    lan_port: int = Field(
        default=0,
        description="内网通道端口；0 表示自动（优先用 access.json 里的监听端口）",
    )
    proxy_port: int = Field(default=3081, description="本机反向代理端口；0 表示自动分配")
    startup_timeout_seconds: int = Field(default=45, description="等待 cloudflared 给出公网地址的超时（秒）")


class FilesConfig(PluginConfigBase):
    """回复文件投递配置。"""

    __ui_label__ = "文件投递"
    __ui_icon__ = "paperclip"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="是否把回复里生成的文件发到当前对话")
    auto_send: bool = Field(default=True, description="QQ 收到回复时自动把文件发过去")
    max_files: int = Field(default=5, description="每条回复最多投递几个文件")
    max_size_mb: int = Field(default=25, description="单个文件大小上限（MB），超过不投递")
    allowed_roots: List[str] = Field(
        default_factory=list,
        description="只投递这些目录下的文件；留空表示只允许插件数据目录（凭据/系统目录始终排除）",
    )


class DshRemoteBridgeConfig(PluginConfigBase):
    """DSH 远程桥接整体配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)
    tunnel: TunnelConfig = Field(default_factory=TunnelConfig)
    files: FilesConfig = Field(default_factory=FilesConfig)


# ---------------------------------------------------------------------------
# 实时流状态
# ---------------------------------------------------------------------------


class StreamState:
    """单个会话的实时流缓冲。

    DSH 的 ``session/follow`` 是无限流，不会自己结束；插件在后台持续消费，
    把归一化后的帧塞进环形缓冲，页面按游标增量取走。
    """

    def __init__(self, session_id: str, capacity: int) -> None:
        self.session_id = session_id
        self.capacity = max(50, int(capacity))
        self.frames: Deque[Dict[str, Any]] = deque(maxlen=self.capacity)
        self.cursor = 0
        self.status = "connecting"
        self.error = ""
        self.task: Optional[asyncio.Task] = None
        self.started_at = time.time()
        self.last_frame_at = 0.0

    def push(self, frame: Dict[str, Any]) -> None:
        self.cursor += 1
        frame["cursor"] = self.cursor
        self.frames.append(frame)
        self.last_frame_at = time.time()

    def since(self, cursor: int) -> List[Dict[str, Any]]:
        if cursor <= 0:
            return list(self.frames)
        return [frame for frame in self.frames if frame.get("cursor", 0) > cursor]

    def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None
        self.status = "closed"


# ---------------------------------------------------------------------------
# 报文归一化
# ---------------------------------------------------------------------------


def _text_of(content: Any) -> str:
    """把 DSH 的 content 数组拼成纯文本。"""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(str(part.get("text") or ""))
        elif isinstance(part, str):
            parts.append(part)
    return "".join(parts)


def normalize_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 DSH 会话事件转成 UI 友好的结构。"""

    if not isinstance(event, dict):
        return None
    kind = str(event.get("type") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    base = {"seq": event.get("seq"), "time": event.get("time")}

    if kind == "user/message":
        return {**base, "kind": "user", "text": _text_of(data.get("content"))}

    if kind == "assistant/message":
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        content = message.get("content") or []
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        tools: List[Dict[str, Any]] = []
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "text":
                    text_parts.append(str(part.get("text") or ""))
                elif part_type == "reasoning":
                    reasoning_parts.append(str(part.get("text") or ""))
                elif part_type == "tool-call":
                    tools.append(
                        {
                            "name": part.get("name") or "",
                            "arguments": part.get("arguments") or "",
                        }
                    )
        return {
            **base,
            "kind": "assistant",
            "text": "".join(text_parts),
            "reasoning": "".join(reasoning_parts),
            "tools": tools,
            "step": data.get("step"),
            "turn": data.get("turn"),
        }

    if kind == "tool/call":
        return {
            **base,
            "kind": "tool-call",
            "name": data.get("name") or "",
            "arguments": data.get("arguments") or "",
        }

    if kind == "tool/result":
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        return {**base, "kind": "tool-result", "text": _text_of(message.get("content"))}

    if kind == "turn/start":
        return {**base, "kind": "status", "text": f"第 {data.get('turn')} 轮开始"}
    if kind == "turn/end":
        return {**base, "kind": "status", "text": f"第 {data.get('turn')} 轮结束"}
    if kind == "step/start":
        return {**base, "kind": "status", "text": f"第 {data.get('turn')} 轮 · 第 {data.get('step')} 步"}
    if kind == "agent/inbox/spliced":
        return None
    if kind == "permission/preset":
        return {**base, "kind": "status", "text": f"权限预设：{data.get('preset')}"}
    return {**base, "kind": "raw", "event": kind, "text": ""}


def normalize_records(records: Any) -> List[Dict[str, Any]]:
    """把 ``session/page`` 的 records 数组转成 UI 消息数组。"""

    items: List[Dict[str, Any]] = []
    if not isinstance(records, list):
        return items
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("type") != "event":
            continue
        event = record.get("event")
        if not isinstance(event, dict):
            continue
        normalized = normalize_event(event)
        if normalized is not None:
            items.append(normalized)
    return items


def normalize_stream_value(value: Any) -> Optional[Dict[str, Any]]:
    """归一化 ``session/follow`` 的单帧。"""

    if not isinstance(value, dict):
        return None
    frame_type = value.get("type")

    if frame_type == "snapshot":
        header = value.get("header") if isinstance(value.get("header"), dict) else {}
        return {
            "kind": "snapshot",
            "messages": normalize_records(value.get("records")),
            "cursor": value.get("cursor"),
            "title": header.get("id") or "",
            "cwd": header.get("cwd") or "",
            "agent_preset": header.get("agentPreset") or "",
        }

    if frame_type == "event":
        event = value.get("event")
        if isinstance(event, dict):
            normalized = normalize_event(event)
            return normalized
        return None

    if frame_type == "assistant-stream":
        frame = value.get("frame") if isinstance(value.get("frame"), dict) else {}
        sub_type = frame.get("type")
        if sub_type == "start":
            return {"kind": "stream-start", "attempt": frame.get("attemptId"), "turn": frame.get("turn")}
        if sub_type == "end":
            return {"kind": "stream-end", "attempt": frame.get("attemptId")}
        if sub_type == "chunk":
            chunk = frame.get("chunk") if isinstance(frame.get("chunk"), dict) else {}
            chunk_type = chunk.get("type")
            if chunk_type == "text-delta":
                return {"kind": "delta", "index": frame.get("index"), "text": chunk.get("text") or ""}
            if chunk_type == "block-start":
                return {"kind": "block-start", "index": frame.get("index"), "block": chunk.get("blockType")}
            if chunk_type == "block-end":
                block = chunk.get("block") if isinstance(chunk.get("block"), dict) else {}
                return {
                    "kind": "block-end",
                    "index": frame.get("index"),
                    "block": block.get("type"),
                    "text": block.get("text") or "",
                }
            return None
        return None

    return None


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class DshRemoteBridgePlugin(QqCommandMixin, MaiBotPlugin):
    """DSH 远程桥接插件。"""

    config_model = DshRemoteBridgeConfig

    def __init__(self) -> None:
        super().__init__()
        self._bridge: Optional[DshBridge] = None
        self._bridge_key: str = ""
        self._endpoint_mode: str = "direct"
        self._endpoint_override: str = ""
        self._streams: Dict[str, StreamState] = {}
        self._last_error: str = ""
        self._last_model_fix: Optional[Dict[str, str]] = None
        self._qq_state: Dict[str, Any] = {}
        self._tunnel_manager = TunnelManager(self)
        self._qq_tasks: List[asyncio.Task] = []

    # ------------------------------------------------------------- 生命周期

    async def on_load(self) -> None:
        cfg = self._connection_config()
        self._bridge = self._build_bridge(cfg)
        self._load_qq_state()
        self.ctx.logger.info(
            f"DSH 远程桥接已加载，目标 {self._bridge.client.base_url}（流式支持：{self._bridge.client.supports_stream()}）"
        )
        tunnel_cfg = self._tunnel_config()
        if tunnel_cfg.auto_start and tunnel_cfg.enabled:
            try:
                status = await self._tunnel_manager.start()
                self.ctx.logger.info(f"公网隧道自动启动：{status.get('public_url') or status.get('error')}")
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning(f"公网隧道自动启动失败: {exc}")

    async def on_unload(self) -> None:
        for state in list(self._streams.values()):
            state.stop()
        self._streams.clear()
        for task in list(self._qq_tasks):
            task.cancel()
        self._qq_tasks.clear()
        try:
            await self._tunnel_manager.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._bridge is not None:
            await self._bridge.client.close()
        self._bridge = None
        self.ctx.logger.info("DSH 远程桥接已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        if scope != "self":
            return
        cfg = self._connection_config()
        if self._bridge is not None:
            await self._bridge.client.close()
        self._bridge = self._build_bridge(cfg)
        self.ctx.logger.info(f"DSH 远程桥接配置已更新，目标 {self._bridge.client.base_url}")

    # --------------------------------------------------------------- 内部

    def _plugin_config(self) -> DshRemoteBridgeConfig:
        raw = self.config
        if isinstance(raw, DshRemoteBridgeConfig):
            return raw
        return DshRemoteBridgeConfig()

    def _connection_config(self) -> ConnectionConfig:
        return self._plugin_config().connection

    def _behavior_config(self) -> BehaviorConfig:
        return self._plugin_config().behavior

    def _whitelist_config(self) -> WhitelistConfig:
        return self._plugin_config().whitelist

    def _tunnel_config(self) -> TunnelConfig:
        return self._plugin_config().tunnel

    def _files_config(self) -> FilesConfig:
        return self._plugin_config().files

    # ------------------------------------------------------- 回复文件投递

    def _collect_reply_files(self, records: Any, since_seq: int = 0) -> List[Dict[str, Any]]:
        """从一批 records 里找出回复中生成的文件。"""

        cfg = self._files_config()
        if not cfg.enabled:
            return []
        return collect_files(
            records or [],
            since_seq=since_seq,
            max_files=max(1, min(20, int(cfg.max_files or 5))),
            max_bytes=max(1, int(cfg.max_size_mb or 25)) * 1024 * 1024,
            allowed_roots=list(cfg.allowed_roots or []),
            fallback_roots=[self._data_dir()],
        )

    async def _deliver_files(self, stream_id: str, files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """把文件按 ``file`` 消息段发到当前对话；失败时退回文本路径。"""

        cfg = self._files_config()
        if not cfg.enabled or not stream_id or not files:
            return []
        delivered: List[Dict[str, Any]] = []
        for item in files[: max(1, min(20, int(cfg.max_files or 5)))]:
            payload = to_file_payload(item)
            ok = False
            error = ""
            try:
                ok = bool(await self.ctx.send.custom("file", payload, stream_id))
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                self.ctx.logger.warning(f"发送文件失败（{item.get('name')}）: {exc}")
            if not ok:
                try:
                    await self.ctx.send.text(f"{item.get('name')}：{item.get('path')}", stream_id)
                    ok = True
                    error = error or "文件消息不受支持，已改发路径"
                except Exception as exc:  # noqa: BLE001
                    error = error or str(exc)
            delivered.append({**item, "delivered": ok, "error": error})
        return delivered

    # ------------------------------------------------------- QQ 状态持久化

    def _qq_state_path(self) -> Path:
        return Path(self.ctx.paths.data_dir) / "qq_state.json"

    def _data_dir(self) -> str:
        """插件数据目录；``files.allowed_roots`` 留空时的默认投递范围。"""

        try:
            return str(Path(self.ctx.paths.data_dir))
        except Exception:  # noqa: BLE001
            return ""

    def _load_qq_state(self) -> None:
        path = self._qq_state_path()
        data: Dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning(f"读取 QQ 状态失败: {exc}")
        for key in ("current", "watch", "list_cache", "baseline", "model_cache", "chat_streams"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        if not isinstance(data.get("extra_allow"), list):
            data["extra_allow"] = []
        self._qq_state = data

    def _save_qq_state(self) -> None:
        path = self._qq_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._qq_state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning(f"保存 QQ 状态失败: {exc}")

    def _bind_chat_stream(self, session_id: str, stream_id: str) -> None:
        """记住某个 DSH 会话是从哪个聊天窗口发起的，文件投递要用。"""

        session_id = (session_id or "").strip()
        stream_id = (stream_id or "").strip()
        if not session_id or not stream_id:
            return
        mapping = self._qq_state.setdefault("chat_streams", {})
        if mapping.get(session_id) != stream_id:
            mapping[session_id] = stream_id
            self._save_qq_state()

    def _chat_stream_for(self, session_id: str) -> str:
        mapping = self._qq_state.get("chat_streams")
        if isinstance(mapping, dict):
            return str(mapping.get((session_id or "").strip()) or "")
        return ""

    @staticmethod
    def _same_origin(left: str, right: str) -> bool:
        """两个地址是否同源（协议 + 主机 + 端口）。"""

        try:
            a, b = urlsplit(left or ""), urlsplit(right or "")
        except ValueError:
            return False
        if not a.hostname or not b.hostname:
            return False

        def port(part: Any) -> int:
            if part.port:
                return int(part.port)
            return 443 if part.scheme == "https" else 80

        return (a.scheme, a.hostname.casefold(), port(a)) == (b.scheme, b.hostname.casefold(), port(b))

    def _build_bridge(self, cfg: ConnectionConfig) -> DshBridge:
        target = self._endpoint_override or cfg.base_url
        token = cfg.token
        if token and not self._same_origin(target, cfg.base_url):
            self.ctx.logger.warning(
                "目标地址 %s 与配置的 DSH 地址 %s 不同源，本次连接不携带访问令牌", target, cfg.base_url
            )
            token = ""
        client = DshClient(
            target,
            token=token,
            timeout=cfg.timeout_seconds,
            verify_tls=cfg.verify_tls,
            proxy=_proxy_for(target, cfg.http_proxy),
        )
        self._bridge_key = client.base_url
        return DshBridge(client)

    def _get_bridge(self) -> DshBridge:
        if self._bridge is None:
            self._bridge = self._build_bridge(self._connection_config())
        return self._bridge

    def _ok(self, **payload: Any) -> Dict[str, Any]:
        return {"ok": True, **payload}

    def _fail(self, error: Exception) -> Dict[str, Any]:
        if isinstance(error, DshError):
            self._last_error = f"{error.code}: {error.message}"
            return {"ok": False, "error": error.to_dict()}
        self._last_error = str(error)
        return {
            "ok": False,
            "error": {"code": "bridge/internal", "message": str(error), "details": {}},
        }

    # ------------------------------------------------------- 模型可用性自愈

    @staticmethod
    def _pick_selection(catalog: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """从模型目录里挑一个真正有适配器的（provider, model）。"""
        if not isinstance(catalog, dict):
            return None
        routable = {str(item) for item in (catalog.get("routableProviders") or []) if item}
        default = catalog.get("default") if isinstance(catalog.get("default"), dict) else {}
        provider = str(default.get("provider") or "")
        model = str(default.get("model") or "")
        if provider and provider in routable and model:
            return {
                "provider": provider,
                "model": model,
                "reasoning_effort": str(default.get("reasoningEffort") or ""),
            }
        for group in catalog.get("groups") or []:
            if not isinstance(group, dict):
                continue
            gid = str(group.get("id") or "")
            if gid not in routable:
                continue
            for item in group.get("models") or []:
                if isinstance(item, dict) and item.get("id"):
                    return {"provider": gid, "model": str(item["id"]), "reasoning_effort": ""}
        return None

    @staticmethod
    def _selection_of(values: Dict[str, Any]) -> Dict[str, Any]:
        selection = values.get("modelSelection") if isinstance(values, dict) else None
        if not isinstance(selection, dict):
            return {}
        for key in ("next", "lastUsed"):
            entry = selection.get(key)
            if isinstance(entry, dict) and entry.get("provider"):
                return entry
        return {}

    async def _ensure_model(
        self,
        bridge: DshBridge,
        session_id: str,
        *,
        pin_default: bool = False,
    ) -> Optional[Dict[str, str]]:
        """会话选的 provider 已无适配器时，自动切到目录里可用的模型。

        返回切换详情（未切换则 None）。任何一步失败都不抛出，交给调用方决定。
        """
        catalog = await bridge.model_catalog()
        routable = {str(item) for item in (catalog.get("routableProviders") or []) if item}
        try:
            projection = await bridge.projections(session_id)
            values = projection.get("values") if isinstance(projection, dict) else {}
        except Exception:  # noqa: BLE001
            values = {}
        current = self._selection_of(values if isinstance(values, dict) else {})
        provider = str(current.get("provider") or "")
        if provider and provider in routable:
            return None
        if not provider and not pin_default:
            return None
        pick = self._pick_selection(catalog)
        if not pick:
            return None
        await bridge.select_model(
            session_id,
            pick["provider"],
            pick["model"],
            pick.get("reasoning_effort") or "",
        )
        self._last_model_fix = {
            "session_id": session_id,
            "from": provider or "(未指定)",
            "to": f"{pick['provider']}/{pick['model']}",
        }
        return self._last_model_fix

    # ------------------------------------------------------------ 查询类 API

    @API("bridge_status", description="返回 DSH 连接状态与插件能力", version="1")
    async def bridge_status(self) -> Dict[str, Any]:
        bridge = self._get_bridge()
        behavior = self._behavior_config()
        payload: Dict[str, Any] = {
            "base_url": bridge.client.base_url,
            "configured_base_url": self._connection_config().base_url,
            "endpoint_mode": self._endpoint_mode,
            "stream_support": bridge.client.supports_stream(),
            "allow_write": behavior.allow_write,
            "active_streams": sorted(self._streams.keys()),
            "last_error": self._last_error,
        }
        try:
            payload.update(await bridge.ping())
        except Exception as exc:  # noqa: BLE001
            payload.update({"ok": False, "reachable": False, "error": self._fail(exc)["error"]})
            return payload
        payload["reachable"] = True
        return payload

    @API("list_sessions", description="列出 DSH 会话", version="1")
    async def list_sessions(self, query: str = "") -> Dict[str, Any]:
        bridge = self._get_bridge()
        behavior = self._behavior_config()
        try:
            data = await bridge.list_sessions()
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)

        items: List[Dict[str, Any]] = []
        keyword = (query or "").strip().lower()
        for item in data.get("items", []):
            if not isinstance(item, dict):
                continue
            projections = item.get("projections") if isinstance(item.get("projections"), dict) else {}
            values = projections.get("values") if isinstance(projections.get("values"), dict) else {}
            title = str(values.get("title") or item.get("sessionId") or "")
            cwd = str(item.get("cwd") or "")
            if keyword and keyword not in title.lower() and keyword not in cwd.lower() and keyword not in str(item.get("sessionId", "")).lower():
                continue
            usage = values.get("tokenUsage") if isinstance(values.get("tokenUsage"), dict) else {}
            items.append(
                {
                    "session_id": item.get("sessionId"),
                    "title": title,
                    "cwd": cwd,
                    "running": bool(item.get("running")),
                    "blank": bool(item.get("blank")),
                    "agent_available": bool(item.get("agentAvailable")),
                    "updated_at": item.get("updatedAt"),
                    "as_of_seq": projections.get("asOfSeq"),
                    "tokens_out": usage.get("outputTokens"),
                    "tokens_in": usage.get("uncachedInputTokens"),
                }
            )
        items.sort(key=lambda row: row.get("updated_at") or 0, reverse=True)
        limited = items[: max(1, behavior.max_sessions)]
        return self._ok(items=limited, total=len(items), shown=len(limited))

    @API("session_detail", description="读取单个会话的投影信息", version="1")
    async def session_detail(self, session_id: str) -> Dict[str, Any]:
        bridge = self._get_bridge()
        try:
            baseline = await bridge.projections(session_id)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        if not isinstance(baseline, dict):
            return self._ok(values={}, as_of_seq=0)
        values = baseline.get("values") if isinstance(baseline.get("values"), dict) else {}
        return self._ok(values=values, as_of_seq=baseline.get("asOfSeq"))

    @API("session_messages", description="分页读取会话历史消息", version="1", public=True)
    async def session_messages(self, session_id: str, limit: int = 0, before_seq: int = 0) -> Dict[str, Any]:
        bridge = self._get_bridge()
        behavior = self._behavior_config()
        count = limit if limit and limit > 0 else behavior.page_size
        try:
            page = await bridge.page(session_id, count, before_seq)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        records = page.get("records") if isinstance(page, dict) else None
        messages = normalize_records(records)
        seqs = [item.get("seq") for item in messages if isinstance(item.get("seq"), int)]
        return self._ok(
            messages=messages,
            files=self._collect_reply_files(records, 0),
            has_more=bool((page or {}).get("hasMore")),
            earliest_seq=min(seqs) if seqs else 0,
        )

    @API("current_session", description="返回当前选中的 DSH 会话与最近生成的文件（供其他插件调用）", version="1", public=True)
    async def current_session(self, user_id: str = "") -> Dict[str, Any]:
        """给办公文件工具箱之类的插件用：拿当前会话和它的产物文件。"""

        uid = str(user_id or "").strip()
        if not uid:
            current_map = self._qq_state.get("current") or {}
            if isinstance(current_map, dict) and current_map:
                uid = str(next(iter(current_map.keys())))
        session_id = self._qq_current(uid) if uid else ""
        if not session_id:
            return self._ok(session_id="", title="", files=[], user_id=uid)

        title = ""
        try:
            detail = await self._get_bridge().projections(session_id)
            title = ((detail or {}).get("values") or {}).get("title") or ""
        except Exception:  # noqa: BLE001
            title = ""

        files: List[Dict[str, Any]] = []
        try:
            page = await self._get_bridge().page(session_id, limit=max(20, self._behavior_config().page_size))
            records = page.get("records") if isinstance(page, dict) else []
            files = self._collect_reply_files(records, 0)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug(f"current_session 读取文件失败: {exc}")

        return self._ok(session_id=session_id, title=title, files=files, user_id=uid)

    @API("model_catalog", description="读取可用模型目录", version="1")
    async def model_catalog(self) -> Dict[str, Any]:
        bridge = self._get_bridge()
        try:
            catalog = await bridge.model_catalog()
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        default = catalog.get("default") if isinstance(catalog, dict) else {}
        groups: List[Dict[str, Any]] = []
        for group in (catalog or {}).get("groups") or []:
            if not isinstance(group, dict):
                continue
            models: List[Dict[str, Any]] = []
            for model in group.get("models") or []:
                if not isinstance(model, dict):
                    continue
                reasoning = model.get("reasoning") if isinstance(model.get("reasoning"), dict) else {}
                efforts = [
                    {"id": effort.get("id"), "name": effort.get("name") or effort.get("id")}
                    for effort in (reasoning.get("efforts") or [])
                    if isinstance(effort, dict)
                ]
                models.append(
                    {
                        "id": model.get("id"),
                        "name": model.get("name") or model.get("id"),
                        "description": model.get("description") or "",
                        "efforts": efforts,
                    }
                )
            groups.append({"id": group.get("id"), "name": group.get("name") or group.get("id"), "models": models})
        return self._ok(
            default=default if isinstance(default, dict) else {},
            groups=groups,
            routable=(catalog or {}).get("routableProviders") or [],
        )

    @API("agent_presets", description="列出可用的 Agent 预设", version="1")
    async def agent_presets(self) -> Dict[str, Any]:
        bridge = self._get_bridge()
        try:
            data = await bridge.agent_presets()
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        presets = data.get("presets") if isinstance(data, dict) else []
        return self._ok(presets=[p for p in (presets or []) if isinstance(p, dict)])

    @API("stream_poll", description="按游标拉取会话实时帧", version="1")
    async def stream_poll(self, session_id: str, cursor: int = 0) -> Dict[str, Any]:
        state = self._streams.get(session_id)
        if state is None:
            return self._ok(active=False, status="idle", cursor=0, frames=[])
        frames = state.since(int(cursor or 0))
        return self._ok(
            active=state.status in ("connecting", "live"),
            status=state.status,
            cursor=state.cursor,
            error=state.error,
            frames=frames[-120:],
        )

    # ------------------------------------------------------------ 操作类 API

    @API("stream_open", description="开始跟随某个会话的实时事件", version="1")
    async def stream_open(self, session_id: str) -> Dict[str, Any]:
        bridge = self._get_bridge()
        if not bridge.client.supports_stream():
            return {
                "ok": False,
                "error": {"code": "bridge/no-stream", "message": "当前环境缺少 WebSocket 支持库", "details": {}},
            }
        existing = self._streams.get(session_id)
        if existing is not None and existing.status in ("connecting", "live"):
            return self._ok(cursor=existing.cursor, status=existing.status)

        behavior = self._behavior_config()
        state = StreamState(session_id, behavior.stream_buffer_size)
        state.task = asyncio.create_task(self._consume_stream(state))
        self._streams[session_id] = state
        return self._ok(cursor=0, status=state.status)

    @API("stream_close", description="停止跟随会话", version="1")
    async def stream_close(self, session_id: str) -> Dict[str, Any]:
        state = self._streams.pop(session_id, None)
        if state is None:
            return self._ok(closed=False)
        state.stop()
        return self._ok(closed=True)

    @API("session_prompt", description="向会话发送一条消息", version="1")
    async def session_prompt(self, session_id: str, text: str, mode: str = "queue") -> Dict[str, Any]:
        if not self._behavior_config().allow_write:
            return {"ok": False, "error": {"code": "bridge/read-only", "message": "写操作已在配置中禁用", "details": {}}}
        content = (text or "").strip()
        if not content:
            return {"ok": False, "error": {"code": "bridge/empty", "message": "消息内容不能为空", "details": {}}}
        bridge = self._get_bridge()
        switched: Optional[Dict[str, str]] = None
        try:
            switched = await self._ensure_model(bridge, session_id)
        except Exception:  # noqa: BLE001
            switched = None
        try:
            result = await bridge.prompt(session_id, content, mode)
        except Exception as exc:  # noqa: BLE001
            # 会话里选的 provider 可能已经被重命名/下线，自动切到可用模型重试一次
            if _is_model_unavailable(exc):
                try:
                    switched = await self._ensure_model(bridge, session_id, pin_default=True)
                    result = await bridge.prompt(session_id, content, mode)
                except Exception as retry_exc:  # noqa: BLE001
                    return self._fail(retry_exc)
            else:
                return self._fail(exc)
        if session_id not in self._streams:
            try:
                await self.stream_open(session_id)
            except Exception:  # noqa: BLE001
                pass
        if switched:
            return self._ok(result=result, model_switched=switched)
        return self._ok(result=result)

    @API("session_cancel", description="中断会话当前生成", version="1")
    async def session_cancel(self, session_id: str) -> Dict[str, Any]:
        if not self._behavior_config().allow_write:
            return {"ok": False, "error": {"code": "bridge/read-only", "message": "写操作已在配置中禁用", "details": {}}}
        bridge = self._get_bridge()
        try:
            result = await bridge.cancel(session_id)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        return self._ok(result=result)

    @API("session_stop", description="停止当前会话：中断生成、清空排队消息并停止跟随", version="1")
    async def session_stop(self, session_id: str = "", clear_queue: bool = True) -> Dict[str, Any]:
        """把「当前会话」真正停下来。

        DSH 的 session/cancel 只停当前这一轮，排队的消息仍会被继续执行；这里额外清空
        next-turn / next-step 队列，并关闭本地跟随流，避免停完又被下一条消息带起来。
        """

        if not self._behavior_config().allow_write:
            return {"ok": False, "error": {"code": "bridge/read-only", "message": "写操作已在配置中禁用", "details": {}}}
        sid = str(session_id or "").strip()
        if not sid:
            current_map = self._qq_state.get("current") or {}
            if isinstance(current_map, dict) and current_map:
                sid = self._qq_current(str(next(iter(current_map.keys()))))
        if not sid:
            return {
                "ok": False,
                "error": {"code": "bridge/no-session", "message": "还没有选择会话", "details": {}},
            }

        bridge = self._get_bridge()
        cancelled: Any = None
        cancel_error = ""
        try:
            cancelled = await bridge.cancel(sid)
        except Exception as exc:  # noqa: BLE001
            cancel_error = str(exc)

        cleared: Dict[str, Any] = {"removed": 0, "failed": []}
        if clear_queue:
            try:
                cleared = await bridge.clear_queue(sid)
            except Exception as exc:  # noqa: BLE001
                cleared = {"removed": 0, "failed": [{"id": "", "error": str(exc)}]}

        closed = False
        try:
            closed = bool((await self.stream_close(sid)).get("closed"))
        except Exception:  # noqa: BLE001
            closed = False

        running = False
        for _ in range(6):
            try:
                running = await bridge.is_running(sid)
            except Exception:  # noqa: BLE001
                running = False
                break
            if not running:
                break
            await asyncio.sleep(0.3)

        payload: Dict[str, Any] = {
            "session_id": sid,
            "cancelled": cancelled,
            "cleared": cleared,
            "stream_closed": closed,
            "running": running,
        }
        if cancel_error:
            payload["cancel_error"] = cancel_error
        return self._ok(**payload)

    @API("send_file", description="把回复里生成的文件发到当前对话窗口", version="1", public=True)
    async def send_file(self, session_id: str, path: str) -> Dict[str, Any]:
        cfg = self._files_config()
        if not cfg.enabled:
            return {"ok": False, "error": {"code": "files/disabled", "message": "文件投递已在配置中禁用", "details": {}}}
        if not self._behavior_config().allow_write:
            return {"ok": False, "error": {"code": "bridge/read-only", "message": "写操作已在配置中禁用", "details": {}}}
        target = _files_module.normalize_path(str(path or ""))
        if target is None:
            return {"ok": False, "error": {"code": "files/not-found", "message": "文件不存在或不是普通文件", "details": {"path": path}}}
        if _files_module.is_denied(target):
            return {"ok": False, "error": {"code": "files/denied", "message": "该目录不允许投递", "details": {"path": str(target)}}}
        if not _files_module.is_allowed_root(
            target, cfg.allowed_roots or [], fallback_roots=[self._data_dir()]
        ):
            return {
                "ok": False,
                "error": {
                    "code": "files/outside-roots",
                    "message": "该文件不在允许投递的目录里（files.allowed_roots，留空时只允许插件数据目录）",
                    "details": {"path": str(target), "allowed_roots": list(cfg.allowed_roots or [])},
                },
            }
        meta = file_meta(target)
        limit = max(1, int(cfg.max_size_mb or 25)) * 1024 * 1024
        if int(meta.get("size") or 0) > limit:
            return {"ok": False, "error": {"code": "files/too-large", "message": f"文件超过 {cfg.max_size_mb} MB 上限", "details": {"path": str(target)}}}
        stream_id = self._chat_stream_for(session_id)
        if not stream_id:
            return {
                "ok": False,
                "error": {
                    "code": "files/no-chat",
                    "message": "该会话还没有绑定聊天窗口，先在 QQ 里用 /dsh ask 发一条消息",
                    "details": {"session_id": session_id},
                },
            }
        delivered = await self._deliver_files(stream_id, [meta])
        return self._ok(delivered=delivered, stream_id=stream_id)

    @API("session_create", description="新建一个 DSH 会话", version="1")
    async def session_create(self, cwd: str = "", agent_preset: str = "") -> Dict[str, Any]:
        if not self._behavior_config().allow_write:
            return {"ok": False, "error": {"code": "bridge/read-only", "message": "写操作已在配置中禁用", "details": {}}}
        bridge = self._get_bridge()
        try:
            result = await bridge.create(cwd, agent_preset)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        session_id = ""
        if isinstance(result, dict):
            session_id = str(result.get("sessionId") or result.get("session_id") or "")
        if session_id:
            try:
                await self._ensure_model(bridge, session_id, pin_default=True)
            except Exception:  # noqa: BLE001
                pass
        return self._ok(result=result)

    @API("session_rename", description="重命名会话", version="1")
    async def session_rename(self, session_id: str, title: str) -> Dict[str, Any]:
        if not self._behavior_config().allow_write:
            return {"ok": False, "error": {"code": "bridge/read-only", "message": "写操作已在配置中禁用", "details": {}}}
        name = (title or "").strip()
        if not name:
            return {"ok": False, "error": {"code": "bridge/empty", "message": "标题不能为空", "details": {}}}
        bridge = self._get_bridge()
        try:
            result = await bridge.rename(session_id, name)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        return self._ok(result=result)

    @API("select_model", description="切换会话使用的模型", version="1")
    async def select_model(
        self,
        session_id: str,
        provider: str,
        model: str,
        reasoning_effort: str = "",
    ) -> Dict[str, Any]:
        if not self._behavior_config().allow_write:
            return {"ok": False, "error": {"code": "bridge/read-only", "message": "写操作已在配置中禁用", "details": {}}}
        bridge = self._get_bridge()
        try:
            result = await bridge.select_model(session_id, provider, model, reasoning_effort)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        return self._ok(result=result)

    @API("test_connection", description="测试任意 DSH 地址是否可用", version="1")
    async def test_connection(self, base_url: str = "") -> Dict[str, Any]:
        target = normalize_base_url(base_url or self._connection_config().base_url)
        connection = self._connection_config()
        token = connection.token
        if token and not self._same_origin(target, connection.base_url):
            # 探活可以打任意地址，但不把访问令牌发到非同源主机
            token = ""
        client = DshClient(
            target,
            token=token,
            timeout=connection.timeout_seconds,
            verify_tls=connection.verify_tls,
            proxy=_proxy_for(target, connection.http_proxy),
        )
        bridge = DshBridge(client)
        started = time.perf_counter()
        try:
            info = await bridge.ping()
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        finally:
            await client.close()
        return self._ok(
            base_url=target,
            auth_sent=bool(token),
            **info,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )

    # ------------------------------------------------------- 隧道 / 白名单 API

    @API("tunnel_status", description="返回公网隧道状态", version="1")
    async def tunnel_status(self) -> Dict[str, Any]:
        return self._ok(**self._tunnel_manager.status())

    @API("tunnel_start", description="启动公网隧道（反向代理 + cloudflared）", version="1")
    async def tunnel_start(self) -> Dict[str, Any]:
        if not self._tunnel_config().enabled:
            return {"ok": False, "error": {"code": "tunnel/disabled", "message": "隧道已在配置中禁用", "details": {}}}
        try:
            status = await self._tunnel_manager.start()
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        return self._ok(**status)

    @API("tunnel_stop", description="停止公网隧道", version="1")
    async def tunnel_stop(self) -> Dict[str, Any]:
        try:
            status = await self._tunnel_manager.stop()
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        return self._ok(**status)

    @API("switch_endpoint", description="切换 DSH 连接地址：direct=内网直连端口 / lan=内网共享 / public=公网隧道 / custom=自定义", version="1")
    async def switch_endpoint(self, mode: str = "direct", url: str = "") -> Dict[str, Any]:
        mode = (mode or "direct").strip().lower()
        connection = self._connection_config()
        tunnel = self._tunnel_manager.status()

        if mode == "direct":
            target = connection.base_url
        elif mode == "lan":
            target = tunnel.get("lan_url", "")
        elif mode == "public":
            target = tunnel.get("public_url", "")
            if not tunnel.get("public_ready"):
                return {
                    "ok": False,
                    "error": {
                        "code": "tunnel/not-running",
                        "message": "公网隧道未运行，请先启动隧道再切到公网地址",
                        "details": tunnel,
                    },
                }
        elif mode == "custom":
            target = url
        else:
            return {
                "ok": False,
                "error": {
                    "code": "endpoint/invalid-mode",
                    "message": "mode 只能是 direct、lan、public 或 custom",
                    "details": {"mode": mode},
                },
            }

        target = (target or "").strip()
        if not target:
            return {
                "ok": False,
                "error": {
                    "code": "endpoint/no-address",
                    "message": "该模式没有可用地址，请检查配置或传入 url",
                    "details": {"mode": mode},
                },
            }

        target = normalize_base_url(target)
        if self._bridge is not None:
            await self._bridge.client.close()
        self._endpoint_mode = mode
        self._endpoint_override = "" if mode == "direct" else target
        self._bridge = self._build_bridge(connection)

        started = time.perf_counter()
        probe_bridge = self._bridge
        temporary: Optional[DshBridge] = None
        if mode == "public":
            # 公网链路走 DNS + TLS，给一个短超时探测，避免超过宿主 10 秒调用上限
            temporary = DshBridge(
                DshClient(
                    target,
                    token=connection.token,
                    timeout=min(float(connection.timeout_seconds), 5.0),
                    verify_tls=connection.verify_tls,
                    proxy=_proxy_for(target, connection.http_proxy),
                )
            )
            probe_bridge = temporary
        try:
            info = await probe_bridge.ping()
        except Exception as exc:  # noqa: BLE001
            payload = self._fail(exc)
            message = "公网地址暂时不可用（域名可能仍在生效），请稍等几秒再切一次" if mode == "public" else str(exc)
            return {
                "ok": False,
                "mode": mode,
                "base_url": target,
                "reachable": False,
                "error": {"code": "endpoint/unreachable", "message": message, "details": payload["error"]},
            }
        finally:
            if temporary is not None:
                await temporary.client.close()
        result: Dict[str, Any] = {"ok": True, "mode": mode, "base_url": target, "reachable": True}
        result.update(info)
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        return result

    @API("whitelist_overview", description="返回 QQ 白名单与命令权限概览", version="1")
    async def whitelist_overview(self) -> Dict[str, Any]:
        cfg = self._whitelist_config()
        return self._ok(
            enabled=cfg.enabled,
            qq_numbers=list(cfg.qq_numbers),
            admins=list(cfg.admins),
            runtime_extra=list(self._qq_state.get("extra_allow", [])),
            ignore_others=cfg.ignore_others,
            command_count=1,
            subcommands=[
                "list",
                "use",
                "new",
                "ask",
                "reply",
                "files",
                "log",
                "stop",
                "watch",
                "rename",
                "model",
                "preset",
                "plugins",
                "plugin",
                "lan",
                "status",
                "tunnel",
                "whoami",
                "allow",
                "deny",
                "allowlist",
                "help",
            ],
        )

    # --------------------------------------------------------------- 后台任务

    async def _consume_stream(self, state: StreamState) -> None:
        bridge = self._get_bridge()
        state.status = "connecting"
        try:
            async for value in bridge.follow(state.session_id):
                state.status = "live"
                raw_event = value.get("event") if isinstance(value, dict) and value.get("type") == "event" else None
                if isinstance(raw_event, dict):
                    files = self._collect_reply_files([{"event": raw_event}], 0)
                    if files:
                        state.push({"kind": "files", "files": files})
                frame = normalize_stream_value(value)
                if frame is not None:
                    state.push(frame)
        except asyncio.CancelledError:
            state.status = "closed"
            raise
        except Exception as exc:  # noqa: BLE001
            state.status = "error"
            state.error = str(exc)
            self.ctx.logger.warning(f"DSH 会话 {state.session_id} 实时流中断: {exc}")
        finally:
            if state.status != "closed":
                state.status = "closed"


def create_plugin() -> DshRemoteBridgePlugin:
    """插件工厂：MaiBot 插件加载器要求的入口。"""

    return DshRemoteBridgePlugin()
