"""QQ 命令层：用小麦（MaiBot）在聊天里操作 DSH。

设计要点
--------
* **白名单**：只有配置里 ``[whitelist].qq_numbers``（以及运行时 ``/dsh allow`` 追加的号码）
  能把命令送到 DSH；其余人的 ``/dsh`` 消息被静默拦截，既不回复也不进模型。
* **每人独立上下文**：每个 QQ 记住自己"当前会话"，互不干扰。
* **异步回复**：``/dsh ask`` 立即返回"已提交"，DSH 生成完由后台任务主动推回群里，
  避免命令 RPC 超时。
* **零宿主依赖**：只用 ``maibot_sdk`` 的 ``Command`` 装饰器与 ``ctx.send``，
  不 import 任何 ``src.*``，可直接拷到别的 MaiBot 实例。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import Command


def _load_sibling(name: str) -> Any:
    """按文件路径加载插件同目录模块（与 plugin.py 内同名函数保持一致的命名）。"""

    base = __name__.rsplit(".", 1)[0] if "." in __name__ else __name__
    module_name = f"{base}.{name}"
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
    spec.loader.exec_module(module)
    return module


_textclean = _load_sibling("textclean")


HELP_TEXT = """DSH 远程命令表
/dsh list [n]            查看会话列表（默认 10 条）
/dsh use <序号|会话ID>    切换当前会话
/dsh new [标题]          新建会话并切过去
/dsh ask <内容>          向当前会话发消息，回复自动推回
/dsh reply [n]           查看最近 n 条 DSH 回复（默认 3）
/dsh log [n]             查看最近 n 轮对话（默认 3）
/dsh files               查看回复里生成的文件
/dsh files <序号>         把该文件重发到当前对话
/dsh stop                停止当前会话（中断生成 + 清空排队 + 停止跟随）
/dsh stop soft           只中断当前生成，保留排队消息
/dsh watch on|off        自动推送新回复开关
/dsh rename <标题>       重命名当前会话
/dsh model               查看可用模型
/dsh model <序号>        切换当前会话模型
/dsh preset              查看可用的 Agent 预设（决定带哪些插件能力）
/dsh preset <名称>        给之后新建的会话指定预设
/dsh plugins [关键词]     查看 DSH 插件及开关状态
/dsh plugins on          一键开启全部 DSH 插件（后台执行，完成再通知）
/dsh plugin on|off <序号|ID>  开启/关闭某个 DSH 插件
/dsh plugin on all       一键开启全部 DSH 插件（同上）
/dsh lan                 启动内网通道并自动切过去（只回「已启动」）
/dsh lan off             关闭内网通道，切回直连
/dsh status              查看桥接与当前会话状态
/dsh tunnel              查看公网隧道
/dsh tunnel on|off       开关公网隧道（开启只回「已启动」，地址用 /dsh tunnel 查）
/dsh whoami              查看自己的权限
/dsh allow <QQ>          把某人加入白名单（管理员）
/dsh deny <QQ>           把某人移出白名单（管理员）
/dsh allowlist           查看白名单（管理员）"""


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _short(error: Any, limit: int = 120) -> str:
    text = str(getattr(error, "message", "") or error or "").strip().replace("\n", " ")
    if len(text) > limit:
        text = text[:limit] + "…"
    return text or "未知原因"


def _is_model_issue(error: Any) -> bool:
    """错误字典/文本是不是「选中的模型没有适配器」。"""

    if isinstance(error, dict):
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")
    else:
        code = str(getattr(error, "code", "") or "")
        message = str(getattr(error, "message", "") or error or "")
    lowered = f"{code} {message}".lower()
    return "model-unavailable" in lowered or "no adapter serves provider" in lowered


_URL_PATTERN = re.compile(r"https?://[^\s'\"）)】\]]+", re.IGNORECASE)
_HOSTPORT_PATTERN = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}\b")


def _scrub_address(text: Any) -> str:
    """把消息里的地址抹掉。

    通道类命令按需求只回「已启动」，但异常信息里常带完整 URL，
    这里统一脱敏，避免地址漏进 QQ 消息。
    """

    cleaned = _URL_PATTERN.sub("<地址已隐藏>", str(text or ""))
    return _HOSTPORT_PATTERN.sub("<地址已隐藏>", cleaned)


_THINK_BLOCK = re.compile(
    r"<(think|thinking|reasoning|analysis|thought)>[\s\S]*?</\1>",
    re.IGNORECASE,
)
_THINK_TAG = re.compile(r"</?(?:think|thinking|reasoning|analysis|thought)>", re.IGNORECASE)


def _size_text(size: int) -> str:
    """人类可读的文件大小。"""

    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _strip_reasoning(text: str) -> str:
    """去掉正文里混进来的思考段落（``<think>…</think>`` 之类的包裹）。"""

    if not text:
        return ""
    cleaned = _THINK_BLOCK.sub("", text)
    cleaned = _THINK_TAG.sub("", cleaned)
    return cleaned.strip()


def _extract_text(content: Any) -> str:
    """从 DSH 的 content 数组里只取「最终回答」的正文。

    DSH 的 assistant content 里会混入 ``reasoning``（思考过程）、``tool-call``
    等块，它们同样带 ``text`` 字段；QQ 侧只应该发回答本身，所以这里按
    ``type == "text"`` 白名单过滤，其余一律丢弃。
    """

    if isinstance(content, str):
        return _strip_reasoning(content)
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return _strip_reasoning("".join(parts))


def _walk_messages(records: Any) -> List[Dict[str, Any]]:
    """把 ``session/page`` 的 records 抽成 {role, text, seq, time} 列表。"""

    out: List[Dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, dict) or record.get("type") != "event":
            continue
        event = record.get("event") or {}
        etype = str(event.get("type") or "")
        data = event.get("data") or {}
        seq = int(event.get("seq") or 0)
        if etype == "user/message":
            text = _extract_text(data.get("content"))
            if text:
                out.append({"role": "user", "text": text, "seq": seq})
        elif etype == "assistant/message":
            message = data.get("message") or {}
            text = _extract_text(message.get("content"))
            if text:
                out.append({"role": "assistant", "text": text, "seq": seq})
    return out


class QqCommandMixin:
    """把 DSH 操作暴露成 QQ 命令。

    宿主类需要提供：``ctx``、``_get_bridge()``、``_behavior_config()``、
    ``_whitelist_config()``、``_tunnel_manager``、``_qq_state``、
    ``_save_qq_state()``。
    """

    # ------------------------------------------------------------ 权限判定

    def _qq_cfg(self) -> Any:
        return self._whitelist_config()

    def _qq_whitelist(self) -> set[str]:
        cfg = self._qq_cfg()
        base = {_digits(item) for item in getattr(cfg, "qq_numbers", []) if _digits(item)}
        extra = {_digits(item) for item in self._qq_state.get("extra_allow", []) if _digits(item)}
        return base | extra

    def _qq_admins(self) -> set[str]:
        cfg = self._qq_cfg()
        return {_digits(item) for item in getattr(cfg, "admins", []) if _digits(item)}

    def _qq_allowed(self, user_id: str, is_local_operator: bool = False) -> bool:
        if is_local_operator:
            return True
        cfg = self._qq_cfg()
        if not getattr(cfg, "enabled", True):
            return True
        number = _digits(user_id)
        if not number:
            return False
        return number in self._qq_whitelist() or number in self._qq_admins()

    def _qq_is_admin(self, user_id: str, is_local_operator: bool = False) -> bool:
        if is_local_operator:
            return True
        number = _digits(user_id)
        return bool(number) and number in self._qq_admins()

    # ------------------------------------------------------------ 会话上下文

    def _qq_current(self, user_id: str) -> str:
        return str(self._qq_state.get("current", {}).get(_digits(user_id), "") or "")

    def _qq_set_current(self, user_id: str, session_id: str) -> None:
        self._qq_state.setdefault("current", {})[_digits(user_id)] = session_id
        self._save_qq_state()

    def _qq_watch(self, user_id: str) -> bool:
        return bool(self._qq_state.get("watch", {}).get(_digits(user_id), False))

    def _qq_set_watch(self, user_id: str, enabled: bool) -> None:
        self._qq_state.setdefault("watch", {})[_digits(user_id)] = bool(enabled)
        self._save_qq_state()

    def _qq_list_cache(self, user_id: str) -> List[Dict[str, Any]]:
        return list(self._qq_state.get("list_cache", {}).get(_digits(user_id), []))

    def _qq_set_list_cache(self, user_id: str, items: List[Dict[str, Any]]) -> None:
        self._qq_state.setdefault("list_cache", {})[_digits(user_id)] = items[:60]
        self._save_qq_state()

    def _qq_baseline(self, user_id: str) -> int:
        return int(self._qq_state.get("baseline", {}).get(_digits(user_id), 0) or 0)

    def _qq_set_baseline(self, user_id: str, seq: int) -> None:
        self._qq_state.setdefault("baseline", {})[_digits(user_id)] = int(seq)
        self._save_qq_state()

    def _qq_resolve_session(self, user_id: str, token: str) -> Tuple[str, str]:
        """把 ``序号`` / ``ID 前缀`` 解析成 sessionId，返回 (session_id, 描述)。"""

        token = (token or "").strip()
        if not token:
            current = self._qq_current(user_id)
            return current, current
        if token.isdigit():
            index = int(token)
            cache = self._qq_list_cache(user_id)
            if 1 <= index <= len(cache):
                item = cache[index - 1]
                return str(item.get("sessionId") or ""), str(item.get("title") or item.get("sessionId") or "")
            return "", ""
        matches = [it for it in self._qq_list_cache(user_id) if str(it.get("sessionId", "")).startswith(token)]
        if len(matches) == 1:
            item = matches[0]
            return str(item.get("sessionId") or ""), str(item.get("title") or "")
        return token, token

    # ------------------------------------------------------------ 发送辅助

    async def _qq_send(self, stream_id: str, text: str) -> None:
        """按 QQ 单条长度切分后发送。"""

        payload = (text or "").strip()
        if not payload or not stream_id:
            return
        limit = max(300, int(getattr(self._qq_cfg(), "max_reply_chars", 1200) or 1200))
        chunks = [payload[i : i + limit] for i in range(0, len(payload), limit)]
        for chunk in chunks:
            try:
                await self.ctx.send.text(chunk, stream_id)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning(f"发送 QQ 消息失败: {exc}")
                break

    # ------------------------------------------------------------ 回复采集

    async def _qq_fetch_messages(self, session_id: str, limit: int = 30) -> List[Dict[str, Any]]:
        bridge = self._get_bridge()
        page = await bridge.page(session_id, limit=max(5, min(200, limit)))
        return _walk_messages(page.get("records") if isinstance(page, dict) else [])

    async def _qq_await_reply(
        self,
        session_id: str,
        baseline_seq: int,
        stream_id: str,
        user_id: str,
        timeout: Optional[float] = None,
        instruction: str = "",
    ) -> None:
        """后台等待 DSH 产出一轮回复并推送回 QQ（只推回答原文，不带前后缀）。"""

        cfg = self._qq_cfg()
        allow_code = _textclean.wants_code(instruction)
        deadline = time.monotonic() + float(timeout or getattr(cfg, "reply_timeout_seconds", 180) or 180)
        sent_seq = baseline_seq
        while time.monotonic() < deadline:
            await asyncio.sleep(2.5)
            try:
                page = await self._get_bridge().page(session_id, limit=40)
            except Exception:  # noqa: BLE001
                continue
            records = page.get("records") if isinstance(page, dict) else []
            messages = _walk_messages(records)
            fresh = [m for m in messages if m["role"] == "assistant" and m["seq"] > sent_seq]
            if fresh:
                sent_seq = max(m["seq"] for m in fresh)
                text = "\n\n".join(m["text"] for m in fresh).strip()
                text = _textclean.clean_reply(
                    text,
                    allow_code=allow_code,
                    keep_empty_note="（这条回复只有结构化内容，已省略）",
                )
                if text:
                    await self._qq_send(stream_id, text)
                files = self._collect_reply_files(records, baseline_seq)
                if files and getattr(self._files_config(), "auto_send", True):
                    await self._deliver_files(stream_id, files)
                self._qq_set_baseline(user_id, sent_seq)
                return
        await self._qq_send(stream_id, "DSH 在超时时间内没有给出回复，可用 /dsh reply 稍后查看。")

    # ------------------------------------------------------------ 命令入口

    @Command(
        "dsh",
        description="用小麦操作 DSH：会话列表、新建、切换、发消息、收回复",
        pattern=(
            r"^\s*(?:(?:@[^\s/／]+|\[reply\]|\[forward\])\s*)*"
            r"(?:/|／)dsh(?:\s+(?P<sub>[A-Za-z]+))?(?:\s+(?P<rest>[\s\S]*))?$"
        ),
    )
    async def dsh_command(self, **kwargs: Any) -> Any:
        user_id = _digits(kwargs.get("user_id"))
        stream_id = str(kwargs.get("stream_id") or "")
        is_local = bool(kwargs.get("is_local_operator"))
        matched = kwargs.get("matched_groups") or {}
        sub = str(matched.get("sub") or "help").strip().lower()
        rest = str(matched.get("rest") or "").strip()

        if not self._qq_allowed(user_id, is_local):
            if getattr(self._qq_cfg(), "ignore_others", True):
                return True, "已忽略", 1
            await self._qq_send(stream_id, "你不在白名单里，无法使用 DSH 命令。")
            return True, "无权限", 1

        handler = getattr(self, f"_dsh_{sub}", None)
        if handler is None:
            await self._qq_send(stream_id, f"未知子命令：{sub}\n\n{HELP_TEXT}")
            return True, "未知子命令", 1
        try:
            await handler(user_id, stream_id, rest, is_local)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning(f"DSH 命令 {sub} 执行失败: {exc}")
            await self._qq_send(stream_id, f"命令执行失败：{exc}")
            return False, str(exc), 1
        return True, "ok", 1

    # ------------------------------------------------------------ 子命令

    async def _dsh_help(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del user_id, rest, is_local
        await self._qq_send(stream_id, HELP_TEXT)

    async def _dsh_status(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del rest, is_local
        bridge = self._get_bridge()
        current = self._qq_current(user_id)
        lines = [f"DSH 地址：{bridge.client.base_url}"]
        try:
            ping = await bridge.ping()
            lines.append(f"连通性：正常（{ping.get('latency_ms')} ms，{ping.get('session_count')} 个会话）")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"连通性：异常（{exc}）")
        if current:
            try:
                detail = await bridge.projections(current)
                title = ((detail or {}).get("values") or {}).get("title") or "(无标题)"
                lines.append(f"当前会话：{title}\n{current}")
                lines.append(f"游标：{(detail or {}).get('asOfSeq')}")
            except Exception:  # noqa: BLE001
                lines.append(f"当前会话：{current}")
        else:
            lines.append("当前会话：未选择（用 /dsh use 或 /dsh new）")
        lines.append(f"自动推送：{'开' if self._qq_watch(user_id) else '关'}")
        preset = str(self._qq_state.get("preset", {}).get(_digits(user_id), ""))
        lines.append(f"新建会话预设：{preset or '未指定（跟随 DSH 默认）'}")
        tunnel = self._tunnel_manager.status()
        if tunnel.get("running"):
            # 公网地址只在用户显式执行 /dsh tunnel 时给出，状态汇总里不主动外泄
            lines.append("公网隧道：运行中（地址用 /dsh tunnel 查）")
        await self._qq_send(stream_id, "\n".join(lines))

    async def _dsh_whoami(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del rest
        role = "管理员" if self._qq_is_admin(user_id, is_local) else ("白名单" if self._qq_allowed(user_id, is_local) else "无权限")
        await self._qq_send(stream_id, f"你的 QQ：{user_id}\n权限：{role}")

    async def _dsh_list(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        count = int(rest) if rest.isdigit() else 10
        count = max(1, min(30, count))
        bridge = self._get_bridge()
        data = await bridge.list_sessions()
        items = list(data.get("items") or [])
        items.sort(key=lambda it: int(it.get("updatedAt") or 0), reverse=True)
        items = items[:count]
        cache: List[Dict[str, Any]] = []
        lines = [f"最近 {len(items)} 个 DSH 会话："]
        current = self._qq_current(user_id)
        for index, item in enumerate(items, start=1):
            sid = str(item.get("sessionId") or "")
            title = str(((item.get("projections") or {}).get("values") or {}).get("title") or "")
            if not title:
                title = str(item.get("cwd") or "(无标题)")
            mark = " ←当前" if sid == current else ""
            flag = "●" if item.get("running") else "○"
            lines.append(f"{index}. {flag} {title}{mark}\n   {sid[:20]}…")
            cache.append({"sessionId": sid, "title": title})
        self._qq_set_list_cache(user_id, cache)
        lines.append("用 /dsh use <序号> 切换会话")
        await self._qq_send(stream_id, "\n".join(lines))

    async def _dsh_use(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        if not rest:
            await self._qq_send(stream_id, "用法：/dsh use <序号|会话ID>")
            return
        session_id, title = self._qq_resolve_session(user_id, rest)
        if not session_id:
            await self._qq_send(stream_id, "没找到这个会话，先 /dsh list 看看序号。")
            return
        try:
            baseline = await self._get_bridge().projections(session_id)
        except Exception as exc:  # noqa: BLE001
            await self._qq_send(stream_id, f"切不过去：{exc}")
            return
        self._qq_set_current(user_id, session_id)
        self._qq_set_baseline(user_id, int((baseline or {}).get("asOfSeq") or 0))
        values = (baseline or {}).get("values") or {}
        await self._qq_send(stream_id, f"已切换当前会话：{values.get('title') or title}\n{session_id}")

    async def _dsh_new(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        bridge = self._get_bridge()
        preset = str(self._qq_state.get("preset", {}).get(_digits(user_id), ""))
        data = await bridge.create(agent_preset=preset)
        session_id = str((data or {}).get("sessionId") or "")
        if not session_id:
            await self._qq_send(stream_id, "新建会话失败：DSH 没有返回 sessionId。")
            return
        used_preset = str((data or {}).get("agentPreset") or preset or "默认")
        try:
            await self._ensure_model(bridge, session_id, pin_default=True)
        except Exception:  # noqa: BLE001
            pass
        if rest:
            try:
                await bridge.rename(session_id, rest)
            except Exception:  # noqa: BLE001
                pass
        self._qq_set_current(user_id, session_id)
        self._qq_set_baseline(user_id, 0)
        await self._qq_send(stream_id, f"收到！已新建并切换到：{rest or '(无标题)'}（预设 {used_preset}）\n{session_id}")

    async def _dsh_ask(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        if not rest:
            await self._qq_send(stream_id, "用法：/dsh ask <要发给 DSH 的内容>")
            return
        if not getattr(self._behavior_config(), "allow_write", True):
            await self._qq_send(stream_id, "插件配置关闭了写操作（behavior.allow_write=false）。")
            return
        session_id = self._qq_current(user_id)
        if not session_id:
            await self._qq_send(stream_id, "还没有选择会话，先 /dsh new 或 /dsh use。")
            return
        bridge = self._get_bridge()
        try:
            baseline = await bridge.projections(session_id)
        except Exception as exc:  # noqa: BLE001
            await self._qq_send(stream_id, f"发送失败：{_short(exc)}")
            return
        seq = int((baseline or {}).get("asOfSeq") or 0)
        result = await self.session_prompt(session_id, rest)
        if not (result or {}).get("ok"):
            error = (result or {}).get("error") or {}
            message = str(error.get("message") or "未知原因")
            if _is_model_issue(error):
                message += "\n（会话里选的模型已不可用，且没能自动切换；用 /dsh model 重新选一个）"
            await self._qq_send(stream_id, f"发送失败：{message}")
            return
        self._qq_set_baseline(user_id, seq)
        self._bind_chat_stream(session_id, stream_id)
        await self._qq_send(stream_id, "收到！")
        asyncio.create_task(self._qq_await_reply(session_id, seq, stream_id, user_id, instruction=rest))

    async def _dsh_reply(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        session_id = self._qq_current(user_id)
        if not session_id:
            await self._qq_send(stream_id, "还没有选择会话。")
            return
        count = int(rest) if rest.isdigit() else 3
        count = max(1, min(10, count))
        messages = [m for m in await self._qq_fetch_messages(session_id, limit=80) if m["role"] == "assistant"]
        if not messages:
            await self._qq_send(stream_id, "当前会话还没有 DSH 回复。")
            return
        picked = messages[-count:]
        body = "\n\n".join(
            _textclean.clean_reply(m["text"], allow_code=True, keep_empty_note="（结构化内容已省略）")
            for m in picked
        ).strip()
        if body:
            await self._qq_send(stream_id, body)
        self._qq_set_baseline(user_id, max(m["seq"] for m in picked))

    async def _dsh_files(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        """列出（或重发）当前会话回复里生成的文件。"""

        del is_local
        session_id = self._qq_current(user_id)
        if not session_id:
            await self._qq_send(stream_id, "还没有选择会话。")
            return
        bridge = self._get_bridge()
        try:
            page = await bridge.page(session_id, limit=80)
        except Exception as exc:  # noqa: BLE001
            await self._qq_send(stream_id, f"读取失败：{_short(exc)}")
            return
        records = page.get("records") if isinstance(page, dict) else []
        files = self._collect_reply_files(records, 0)
        if not files:
            await self._qq_send(stream_id, "当前会话最近的消息里没有生成文件。")
            return
        token = (rest or "").strip()
        if token.isdigit():
            index = int(token)
            if not 1 <= index <= len(files):
                await self._qq_send(stream_id, f"序号超出范围：1~{len(files)}")
                return
            await self._deliver_files(stream_id, [files[index - 1]])
            return
        lines = [
            f"{i}. {item['name']}（{_size_text(int(item.get('size') or 0))}）"
            for i, item in enumerate(files, 1)
        ]
        await self._qq_send(stream_id, "最近生成的文件：\n" + "\n".join(lines) + "\n\n用 /dsh files <序号> 重发。")

    async def _dsh_log(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        session_id = self._qq_current(user_id)
        if not session_id:
            await self._qq_send(stream_id, "还没有选择会话。")
            return
        rounds = int(rest) if rest.isdigit() else 3
        rounds = max(1, min(10, rounds))
        messages = await self._qq_fetch_messages(session_id, limit=100)
        picked = messages[-rounds * 2 :] if messages else []
        if not picked:
            await self._qq_send(stream_id, "当前会话还没有消息。")
            return
        lines = []
        for item in picked:
            who = "我" if item["role"] == "user" else "DSH"
            text = item["text"]
            if item["role"] == "assistant":
                text = _textclean.clean_reply(text, allow_code=False, keep_empty_note="（结构化内容已省略）")
            if len(text) > 400:
                text = text[:400] + "…"
            lines.append(f"{who}：{text}")
        await self._qq_send(stream_id, "\n\n".join(lines))

    async def _dsh_stop(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        session_id = self._qq_current(user_id)
        if not session_id:
            await self._qq_send(stream_id, "还没有选择会话。先用 /dsh list 选一个。")
            return
        soft = rest.strip().lower() in {"soft", "中断", "只中断"}
        result = await self.session_stop(session_id, clear_queue=not soft)
        if not result.get("ok"):
            message = ((result.get("error") or {}).get("message")) or "未知错误"
            await self._qq_send(stream_id, f"停止失败：{message}")
            return
        if soft:
            await self._qq_send(stream_id, "已中断当前生成。")
            return
        removed = int(((result.get("cleared") or {}).get("removed")) or 0)
        detail = f"，清掉了 {removed} 条排队消息" if removed else ""
        head = "已停止当前会话" if not result.get("running") else "已发送停止，正在收尾"
        await self._qq_send(stream_id, f"{head}{detail}。")

    async def _dsh_watch(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        action = rest.strip().lower()
        if action in {"on", "开", "1", "true"}:
            self._qq_set_watch(user_id, True)
            await self._qq_send(stream_id, "收到！")
        elif action in {"off", "关", "0", "false"}:
            self._qq_set_watch(user_id, False)
            await self._qq_send(stream_id, "收到！")
        else:
            await self._qq_send(stream_id, f"自动推送当前：{'开' if self._qq_watch(user_id) else '关'}\n用法：/dsh watch on|off")

    async def _dsh_rename(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        session_id = self._qq_current(user_id)
        if not session_id or not rest:
            await self._qq_send(stream_id, "用法：/dsh rename <新标题>（需先选择会话）")
            return
        data = await self._get_bridge().rename(session_id, rest)
        await self._qq_send(stream_id, "收到！")

    async def _dsh_model(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        bridge = self._get_bridge()
        catalog = await bridge.model_catalog()
        options: List[Dict[str, str]] = []
        lines: List[str] = []
        routable = {str(item) for item in ((catalog or {}).get("routableProviders") or []) if item}
        for group in (catalog or {}).get("groups") or []:
            provider = str(group.get("id") or "")
            if routable and provider not in routable:
                continue
            for model in group.get("models") or []:
                options.append(
                    {
                        "provider": provider,
                        "model": str(model.get("id") or ""),
                        "name": str(model.get("name") or model.get("id") or ""),
                    }
                )
        if not rest:
            if not options:
                await self._qq_send(stream_id, "DSH 没有返回可用模型。")
                return
            self._qq_state.setdefault("model_cache", {})[_digits(user_id)] = options[:40]
            self._save_qq_state()
            default = (catalog or {}).get("default") or {}
            lines.append(f"默认模型：{default.get('provider')}/{default.get('model')}")
            for index, item in enumerate(options[:20], start=1):
                lines.append(f"{index}. {item['name']}（{item['provider']}/{item['model']}）")
            lines.append("用 /dsh model <序号> 切换")
            await self._qq_send(stream_id, "\n".join(lines))
            return
        if not rest.isdigit():
            await self._qq_send(stream_id, "用法：/dsh model <序号>")
            return
        cache = self._qq_state.get("model_cache", {}).get(_digits(user_id), [])
        index = int(rest)
        if not (1 <= index <= len(cache)):
            await self._qq_send(stream_id, "序号超出范围，先 /dsh model 看列表。")
            return
        session_id = self._qq_current(user_id)
        if not session_id:
            await self._qq_send(stream_id, "请先选择会话。")
            return
        chosen = cache[index - 1]
        try:
            await bridge.select_model(session_id, chosen["provider"], chosen["model"])
        except Exception as exc:  # noqa: BLE001
            await self._qq_send(stream_id, f"切换模型失败：{_short(exc)}")
            return
        await self._qq_send(stream_id, f"已切换模型：{chosen['name']}")

    async def _dsh_preset(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        try:
            data = await self._get_bridge().agent_presets()
        except Exception as exc:  # noqa: BLE001
            await self._qq_send(stream_id, f"读取预设失败：{exc}")
            return
        presets = (data or {}).get("presets") if isinstance(data, dict) else None
        presets = presets if isinstance(presets, list) else []
        current = str(self._qq_state.get("preset", {}).get(_digits(user_id), ""))
        if not rest:
            lines = [f"新建会话用的预设：{current or '未指定（跟随 DSH 默认）'}"]
            for index, item in enumerate(presets[:20], start=1):
                mark = " ←当前" if current and str(item.get("id") or "") == current else ""
                flag = " ★默认" if item.get("isDefault") else ""
                lines.append(f"{index}. {item.get('id')}{flag}{mark}")
            lines.append("用 /dsh preset <名称|序号> 指定，之后 /dsh new 生效")
            await self._qq_send(stream_id, "\n".join(lines))
            return
        target = rest.strip()
        chosen = ""
        if target.isdigit():
            index = int(target)
            if 1 <= index <= len(presets):
                chosen = str(presets[index - 1].get("id") or "")
        elif any(str(item.get("id") or "") == target for item in presets):
            chosen = target
        if not chosen:
            await self._qq_send(stream_id, "没找到这个预设，先 /dsh preset 看列表。")
            return
        self._qq_state.setdefault("preset", {})[_digits(user_id)] = chosen
        self._save_qq_state()
        await self._qq_send(stream_id, f"收到！之后新建的会话使用预设：{chosen}")

    async def _dsh_plugins(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        keyword = rest.strip().lower()
        if keyword in {"on", "all", "on all", "开", "开启", "全部", "全部开启"}:
            # /dsh plugins on 等价于 /dsh plugin on all，顺手做个别名。
            await self._dsh_plugin(user_id, stream_id, "on all", is_local)
            return
        del is_local
        try:
            data = await self._get_bridge().list_plugins()
        except Exception as exc:  # noqa: BLE001
            await self._qq_send(stream_id, f"读取插件列表失败：{exc}")
            return
        items = data if isinstance(data, list) else []
        keyword = rest.strip().lower()
        if keyword:
            items = [
                item
                for item in items
                if keyword in str(item.get("moduleName") or "").lower()
                or keyword in str(item.get("entryId") or "").lower()
            ]
        cache = [
            {
                "entryId": str(item.get("entryId") or ""),
                "moduleName": str(item.get("moduleName") or ""),
                "enabled": bool(item.get("enabled")),
            }
            for item in items[:60]
        ]
        self._qq_state.setdefault("plugin_cache", {})[_digits(user_id)] = cache
        self._save_qq_state()
        if not cache:
            await self._qq_send(stream_id, f"没有匹配「{rest}」的插件。")
            return
        lines = [f"DSH 插件：{len(items)} 个匹配，显示前 {len(cache)} 个"]
        for index, item in enumerate(cache, start=1):
            flag = "●" if item["enabled"] else "○"
            lines.append(f"{index}. {flag} {item['moduleName']}\n   id: {item['entryId']}")
        lines.append("●开启 ○关闭；用 /dsh plugin on|off <序号|ID> 开关")
        await self._qq_send(stream_id, "\n".join(lines))

    async def _dsh_plugin(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        words = rest.split()
        verbs = {"on", "off", "开", "关", "enable", "disable"}
        action = ""
        target = ""
        if len(words) >= 2 and words[0].lower() in verbs:
            action, target = words[0].lower(), " ".join(words[1:]).strip()
        elif len(words) >= 2 and words[-1].lower() in verbs:
            action, target = words[-1].lower(), " ".join(words[:-1]).strip()
        if not action or not target:
            await self._qq_send(stream_id, "用法：/dsh plugin on|off <序号|ID>")
            return
        enabled = action in {"on", "开", "enable"}
        if target.lower() in {"all", "*", "全部", "所有", "全部插件", "everything"}:
            if not enabled:
                await self._qq_send(
                    stream_id,
                    "不支持一键关闭全部插件（会把正在用的也一起关掉）。请先 /dsh plugins 看列表，再逐个关。",
                )
                return
            await self._enable_all_plugins(stream_id)
            return
        cache = self._qq_state.get("plugin_cache", {}).get(_digits(user_id), [])
        entry_id = ""
        if target.isdigit():
            index = int(target)
            if 1 <= index <= len(cache):
                entry_id = str(cache[index - 1].get("entryId") or "")
        else:
            entry_id = target
        if not entry_id:
            await self._qq_send(stream_id, "序号超出范围，先 /dsh plugins 看列表。")
            return
        try:
            result = await self._get_bridge().set_plugin_enabled(entry_id, enabled)
        except Exception as exc:  # noqa: BLE001
            await self._qq_send(stream_id, f"开关失败：{exc}")
            return
        warnings = (result or {}).get("warnings") or []
        suffix = f"（{len(warnings)} 条警告，详见 DSH 日志）" if warnings else ""
        await self._qq_send(stream_id, f"收到！已{'开启' if enabled else '关闭'} {entry_id}{suffix}")

    async def _enable_all_plugins(self, stream_id: str) -> None:
        """一键开启全部 DSH 插件（后台跑，避免超过宿主的单次调用等待）。"""

        running = getattr(self, "_qq_enable_all_task", None)
        if running is not None and not running.done():
            await self._qq_send(stream_id, "已经在开启中了，稍等。")
            return
        task = asyncio.create_task(self._run_enable_all(stream_id))
        self._qq_enable_all_task = task
        self._qq_tasks.append(task)

        def _forget(done: "asyncio.Task") -> None:
            try:
                self._qq_tasks.remove(done)
            except ValueError:
                pass

        task.add_done_callback(_forget)
        await self._qq_send(stream_id, "收到！正在开启全部插件…")

    async def _run_enable_all(self, stream_id: str) -> None:
        bridge = self._get_bridge()
        try:
            data = await bridge.list_plugins()
        except Exception as exc:  # noqa: BLE001
            await self._qq_send(stream_id, f"读取插件列表失败：{_scrub_address(exc)}")
            return
        items = data if isinstance(data, list) else []
        pending: List[str] = []
        already = 0
        skipped = 0
        for item in items:
            entry_id = str(item.get("entryId") or "")
            if not entry_id:
                continue
            if item.get("enabled"):
                already += 1
                continue
            if item.get("readOnly") or item.get("readOnlyReason"):
                skipped += 1
                continue
            pending.append(entry_id)
        if not pending:
            await self._qq_send(stream_id, f"收到！全部插件都已经开着（共 {already} 个）")
            return
        ok = 0
        failed: List[str] = []
        for entry_id in pending:
            try:
                await bridge.set_plugin_enabled(entry_id, True)
                ok += 1
            except Exception:  # noqa: BLE001 - 单个失败不影响其余
                failed.append(entry_id)
            await asyncio.sleep(0.05)
        parts = [f"插件开启完成：成功 {ok} 个"]
        if already:
            parts.append(f"原本已开启 {already} 个")
        if skipped:
            parts.append(f"只读跳过 {skipped} 个")
        if failed:
            parts.append(f"失败 {len(failed)} 个（{', '.join(failed[:5])}）")
        await self._qq_send(stream_id, "，".join(parts))

    async def _dsh_lan(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        """启动内网通道：切到局域网地址并验证，成功只回「已启动」。"""

        del user_id, is_local
        action = rest.strip().lower()
        if action in {"off", "stop", "关", "关闭"}:
            result = await self.switch_endpoint(mode="direct")
            if result.get("ok"):
                await self._qq_send(stream_id, "已关闭")
            else:
                message = (result.get("error") or {}).get("message") or "未知原因"
                await self._qq_send(stream_id, "关闭失败：" + _scrub_address(message))
            return
        result = await self.switch_endpoint(mode="lan")
        if result.get("ok"):
            await self._qq_send(stream_id, "已启动")
            return
        message = (result.get("error") or {}).get("message") or "未知原因"
        await self._qq_send(stream_id, "内网通道启动失败：" + _scrub_address(message))

    async def _dsh_tunnel(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        action = rest.strip().lower()
        manager = self._tunnel_manager
        if action in {"on", "start", "开"}:
            if not getattr(self._tunnel_config(), "enabled", True):
                await self._qq_send(stream_id, "插件配置里没有启用隧道（tunnel.enabled=false）。")
                return
            status = await manager.start()
            if status.get("running"):
                # 按要求只回「已启动」，公网地址不往 QQ 里发（用 /dsh tunnel 查）。
                await self._qq_send(stream_id, "已启动")
            else:
                await self._qq_send(stream_id, "隧道启动失败：" + _scrub_address(status.get("error")))
            return
        if action in {"off", "stop", "关"}:
            status = await manager.stop()
            await self._qq_send(stream_id, f"隧道已关闭（{status.get('state')}）。")
            return
        status = manager.status()
        lines = [
            f"状态：{status.get('state')}",
            f"公网地址：{status.get('public_url') or '（未开启）'}",
            f"本地代理：{status.get('local_url') or '（未启动）'}",
            f"目标：{status.get('target')}",
        ]
        if status.get("error"):
            lines.append(f"错误：{status.get('error')}")
        await self._qq_send(stream_id, "\n".join(lines))

    # ------------------------------------------------------------ 管理员

    async def _dsh_allow(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        if not self._qq_is_admin(user_id, is_local):
            await self._qq_send(stream_id, "只有管理员可以改白名单。")
            return
        target = _digits(rest)
        if not target:
            await self._qq_send(stream_id, "用法：/dsh allow <QQ号>")
            return
        extra = self._qq_state.setdefault("extra_allow", [])
        if target not in extra:
            extra.append(target)
            self._save_qq_state()
        await self._qq_send(stream_id, f"已加入白名单：{target}")

    async def _dsh_deny(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        if not self._qq_is_admin(user_id, is_local):
            await self._qq_send(stream_id, "只有管理员可以改白名单。")
            return
        target = _digits(rest)
        if not target:
            await self._qq_send(stream_id, "用法：/dsh deny <QQ号>")
            return
        extra = self._qq_state.setdefault("extra_allow", [])
        if target in extra:
            extra.remove(target)
            self._save_qq_state()
        await self._qq_send(stream_id, f"已移出白名单：{target}（配置里的号码需要在 config.toml 改）")

    async def _dsh_allowlist(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del rest
        if not self._qq_is_admin(user_id, is_local):
            await self._qq_send(stream_id, "只有管理员可以查看白名单。")
            return
        cfg = self._qq_cfg()
        lines = ["配置文件白名单：" + (", ".join(getattr(cfg, "qq_numbers", [])) or "（空）")]
        lines.append("运行时追加：" + (", ".join(self._qq_state.get("extra_allow", [])) or "（空）"))
        lines.append("管理员：" + (", ".join(getattr(cfg, "admins", [])) or "（空）"))
        await self._qq_send(stream_id, "\n".join(lines))
