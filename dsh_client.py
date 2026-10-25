"""DSH（DeepSeek Harness）远程 RPC 客户端。

本模块只依赖 Python 标准库；如果运行环境里存在 ``aiohttp``，会自动启用
更低延迟的 HTTP / WebSocket 实现，否则自动降级：

* HTTP 层：``aiohttp`` → 标准库 ``urllib``（在线程池里跑）
* 流式层：``aiohttp`` WebSocket → ``websockets`` → 不可用（流式功能自动关闭）

DSH 的协议要点（0.1.7-rc.1 实测）：

HTTP 单次调用::

    POST <base>/api/<namespace>/<method>
    Content-Type: application/json
    {"type":"client-request","rpcId":"<uuid>","method":"<ns>/<method>",
     "payload":{"args":{...}}}

    → {"type":"server-response","rpcId":"<uuid>",
       "result":{"ok":true,"value":...}}
    → {"type":"server-response","rpcId":"<uuid>",
       "result":{"ok":false,"error":{"code":"gateway/...","message":"...","details":{...}}}}

流式调用走 WebSocket ``<ws_base>/api/remote.mux``::

    客户端 → {"type":"open","streamId":"<str>","endpoint":"session/follow","payload":{"args":{...}}}
    服务端 → {"type":"item","streamId":"<str>","value":...}
            {"type":"error","streamId":"<str>","error":{...}}
            {"type":"end","streamId":"<str>"}

注意 ``streamId`` 必须是**非空字符串**，数字会被服务端以 1008 断开。
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import quote, urlsplit, urlunsplit

try:  # pragma: no cover - 环境相关
    import aiohttp  # type: ignore

    HAS_AIOHTTP = True
except Exception:  # pragma: no cover
    aiohttp = None  # type: ignore
    HAS_AIOHTTP = False

try:  # pragma: no cover - 环境相关
    import websockets  # type: ignore

    HAS_WEBSOCKETS = True
except Exception:  # pragma: no cover
    websockets = None  # type: ignore
    HAS_WEBSOCKETS = False


class DshError(Exception):
    """DSH 返回的结构化错误。"""

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def normalize_base_url(raw: str) -> str:
    """把用户填写的地址规整成 ``scheme://host[:port]`` 形式。"""

    text = (raw or "").strip()
    if not text:
        return "http://127.0.0.1:3080"
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    path = parts.path.rstrip("/")
    if path.endswith("/api"):
        path = path[:-4]
    return urlunsplit((parts.scheme or "http", parts.netloc, path, "", ""))


def ws_url_for(base_url: str) -> str:
    """把 HTTP 基地址换算成 WebSocket 基地址。"""

    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + "/api/remote.mux", "", ""))


def api_url_for(base_url: str, endpoint: str) -> str:
    return f"{base_url.rstrip('/')}/api/{endpoint}"


def _build_envelope(endpoint: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "client-request",
        "rpcId": uuid.uuid4().hex,
        "method": endpoint,
        "payload": {"args": args or {}},
    }


def _parse_envelope(payload: Any) -> Any:
    """从 ``{"type":"server-response","result":{...}}`` 里取出 value 或抛错。"""

    if not isinstance(payload, dict):
        raise DshError("bridge/bad-response", f"响应不是 JSON 对象: {type(payload).__name__}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise DshError("bridge/bad-response", "响应缺少 result 字段", {"raw": str(payload)[:400]})
    if result.get("ok") is True:
        return result.get("value")
    error = result.get("error") or {}
    raise DshError(
        str(error.get("code") or "gateway/internal"),
        str(error.get("message") or "DSH 调用失败"),
        error.get("details") if isinstance(error.get("details"), dict) else {},
    )


class DshClient:
    """一个 DSH 端点的 RPC 客户端（无状态，可复用）。"""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 12.0,
        verify_tls: bool = True,
        proxy: str = "",
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.token = (token or "").strip()
        self.timeout = max(1.0, float(timeout))
        self.verify_tls = bool(verify_tls)
        # 公网链路（trycloudflare）常被本机 DNS 污染，需要走 HTTP 代理才能解析，
        # 内网直连时留空即可。
        self.proxy = (proxy or "").strip()
        self._session = None  # type: Any
        self._session_loop = None  # type: Any

    # ------------------------------------------------------------------ HTTP

    async def _get_session(self):
        if not HAS_AIOHTTP:
            return None
        loop = asyncio.get_running_loop()
        if self._session is not None and self._session_loop is loop and not self._session.closed:
            return self._session
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        connector = None
        if self.base_url.startswith("https") and not self.verify_tls:
            connector = aiohttp.TCPConnector(ssl=False)
        self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        self._session_loop = loop
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-DSH-Token"] = self.token
        return headers

    async def rpc(self, endpoint: str, args: Optional[Dict[str, Any]] = None) -> Any:
        """发起一次单次调用。``endpoint`` 形如 ``session/list``。"""

        url = api_url_for(self.base_url, endpoint)
        body = json.dumps(_build_envelope(endpoint, args or {}), ensure_ascii=False).encode("utf-8")

        if HAS_AIOHTTP:
            session = await self._get_session()
            try:
                async with session.post(url, data=body, headers=self._headers(), proxy=self.proxy or None) as resp:
                    # DSH 返回的 Content-Type 只有 application/json，没有 charset，
                    # 交给 aiohttp 自动探测会把中文解成乱码，这里强制按 UTF-8 解码。
                    text = (await resp.read()).decode("utf-8", "replace")
                    if resp.status != 200:
                        raise DshError(
                            f"bridge/http-{resp.status}",
                            f"DSH 返回 HTTP {resp.status}",
                            {"body": text[:800], "endpoint": endpoint},
                        )
            except DshError:
                raise
            except asyncio.TimeoutError as exc:
                raise DshError("bridge/timeout", f"DSH 请求超时（{self.timeout:.0f}s）", {"endpoint": endpoint}) from exc
            except Exception as exc:
                raise DshError("bridge/network", f"连不上 DSH: {exc}", {"endpoint": endpoint, "url": url}) from exc
        else:  # pragma: no cover - 无 aiohttp 的降级路径
            text = await asyncio.to_thread(self._post_blocking, url, body)
            if isinstance(text, tuple):
                status, text = text
                if status != 200:
                    raise DshError(
                        f"bridge/http-{status}",
                        f"DSH 返回 HTTP {status}",
                        {"body": text[:800], "endpoint": endpoint},
                    )

        try:
            payload = json.loads(text)
        except Exception as exc:
            raise DshError(
                "bridge/bad-response",
                "DSH 响应不是合法 JSON",
                {"body": text[:800], "endpoint": endpoint},
            ) from exc
        return _parse_envelope(payload)

    def _post_blocking(self, url: str, body: bytes):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, data=body, headers=self._headers(), method="POST")
        context = None
        if url.startswith("https") and not self.verify_tls:
            context = ssl._create_unverified_context()  # noqa: SLF001
        opener = None
        if self.proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            )
        try:
            with (opener.open(request, timeout=self.timeout) if opener is not None else urllib.request.urlopen(request, timeout=self.timeout, context=context)) as response:  # noqa: S310
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    # ------------------------------------------------------------- WebSocket

    def supports_stream(self) -> bool:
        return HAS_AIOHTTP or HAS_WEBSOCKETS

    async def stream(
        self,
        endpoint: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        stream_id: Optional[str] = None,
    ) -> AsyncIterator[Any]:
        """打开一条 DSH 流，逐条 yield 服务端推送的 ``value``。"""

        if not self.supports_stream():
            raise DshError("bridge/no-stream", "当前环境缺少 aiohttp / websockets，无法建立流式连接")

        url = ws_url_for(self.base_url)
        sid = stream_id or uuid.uuid4().hex
        open_frame = {
            "type": "open",
            "streamId": sid,
            "endpoint": endpoint,
            "payload": {"args": args or {}},
        }

        if HAS_AIOHTTP:
            session = await self._get_session()
            ssl_ctx = False if (self.base_url.startswith("https") and not self.verify_tls) else None
            try:
                async with session.ws_connect(
                    url,
                    heartbeat=25.0,
                    max_msg_size=0,
                    ssl=ssl_ctx,
                    headers=self._headers() if self.token else None,
                    proxy=self.proxy or None,
                ) as ws:
                    await ws.send_str(json.dumps(open_frame, ensure_ascii=False))
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            try:
                                frame = json.loads(message.data)
                            except Exception:
                                continue
                            if not isinstance(frame, dict):
                                continue
                            kind = frame.get("type")
                            if kind == "item":
                                yield frame.get("value")
                            elif kind == "error":
                                error = frame.get("error") or {}
                                raise DshError(
                                    str(error.get("code") or "gateway/internal"),
                                    str(error.get("message") or "DSH 流错误"),
                                    error.get("details") if isinstance(error.get("details"), dict) else {},
                                )
                            elif kind == "end":
                                return
                        elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            return
            except DshError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise DshError("bridge/stream", f"流式连接中断: {exc}", {"endpoint": endpoint, "url": url}) from exc
            return

        async with websockets.connect(url, ping_interval=25.0, max_size=None) as ws:  # pragma: no cover
            await ws.send(json.dumps(open_frame, ensure_ascii=False))
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(frame, dict):
                    continue
                kind = frame.get("type")
                if kind == "item":
                    yield frame.get("value")
                elif kind == "error":
                    error = frame.get("error") or {}
                    raise DshError(
                        str(error.get("code") or "gateway/internal"),
                        str(error.get("message") or "DSH 流错误"),
                    )
                elif kind == "end":
                    return


# --------------------------------------------------------------------------
# 语义化封装：只暴露插件真正要用到的 DSH 端点
# --------------------------------------------------------------------------


class DshBridge:
    """把 DSH 的原始端点包装成插件需要的业务方法。"""

    def __init__(self, client: DshClient) -> None:
        self.client = client

    # ---- 读 ----

    async def ping(self) -> Dict[str, Any]:
        started = time.perf_counter()
        value = await self.client.rpc("session/list", {"_request": {}})
        latency = (time.perf_counter() - started) * 1000.0
        items = (value or {}).get("items") or []
        return {"ok": True, "latency_ms": round(latency, 1), "session_count": len(items)}

    async def list_sessions(self) -> Dict[str, Any]:
        value = await self.client.rpc("session/list", {"_request": {}})
        items = (value or {}).get("items") or []
        return {"items": items}

    async def projections(self, session_id: str) -> Any:
        return await self.client.rpc("session/projections", {"request": {"sessionId": session_id}})

    async def page(self, session_id: str, limit: int = 60, before_seq: int = 0) -> Dict[str, Any]:
        baseline = await self.projections(session_id)
        through_seq = int((baseline or {}).get("asOfSeq") or 0)
        request: Dict[str, Any] = {
            "address": {"kind": "session", "sessionId": session_id},
            "throughSeq": through_seq,
            "maxMessages": max(1, min(400, int(limit))),
        }
        if before_seq and int(before_seq) > 0:
            request["beforeSeq"] = int(before_seq)
        return await self.client.rpc("session/page", {"request": request})

    async def model_catalog(self) -> Dict[str, Any]:
        return await self.client.rpc("session/modelCatalog", {})

    async def agent_presets(self) -> Dict[str, Any]:
        return await self.client.rpc("agentPresets/list", {})

    # ---- 插件管理 ----

    async def list_plugins(self) -> Any:
        return await self.client.rpc("pluginManager/listPlugins", {})

    async def set_plugin_enabled(self, entry_id: str, enabled: bool) -> Dict[str, Any]:
        return await self.client.rpc(
            "pluginManager/setPluginEnabled",
            {"id": str(entry_id), "enabled": bool(enabled)},
        )

    # ---- 写 ----

    async def create(self, cwd: str = "", agent_preset: str = "") -> Dict[str, Any]:
        request: Dict[str, Any] = {}
        if cwd.strip():
            request["cwd"] = cwd.strip()
        if agent_preset.strip():
            request["agentPreset"] = agent_preset.strip()
        return await self.client.rpc("session/create", {"request": request})

    async def rename(self, session_id: str, title: str) -> Dict[str, Any]:
        return await self.client.rpc("session/rename", {"request": {"sessionId": session_id, "title": title}})

    async def prompt(self, session_id: str, text: str, mode: str = "queue") -> Dict[str, Any]:
        request = {
            "requestId": uuid.uuid4().hex,
            "sessionId": session_id,
            "mode": "steer" if mode == "steer" else "queue",
            "content": [{"type": "text", "text": text}],
        }
        return await self.client.rpc("session/prompt", {"request": request})

    async def cancel(self, session_id: str) -> Dict[str, Any]:
        return await self.client.rpc("session/cancel", {"request": {"sessionId": session_id}})

    async def pending_items(self, session_id: str) -> Dict[str, List[Dict[str, Any]]]:
        """读取会话里还排着队的消息（next-turn 下一轮 / next-step 下一步）。"""
        projection = await self.projections(session_id)
        inbox = ((projection or {}).get("values") or {}).get("inbox") or {}
        return {
            "next-step": list(inbox.get("next-step") or []),
            "next-turn": list(inbox.get("next-turn") or []),
        }

    async def update_queue(self, session_id: str, item_id: str, action: Dict[str, Any]) -> Dict[str, Any]:
        return await self.client.rpc(
            "session/updateQueue",
            {"request": {"sessionId": session_id, "itemId": str(item_id), "action": action}},
        )

    async def clear_queue(self, session_id: str) -> Dict[str, Any]:
        """清空会话排队中的消息，返回清掉的条数与失败项。"""
        items = await self.pending_items(session_id)
        removed = 0
        failed: List[Dict[str, str]] = []
        # 先清下一步的，再清下一轮的，避免清的过程中被提升到前一层
        for key in ("next-step", "next-turn"):
            for item in items.get(key) or []:
                item_id = item.get("id")
                if not item_id:
                    continue
                try:
                    await self.update_queue(session_id, str(item_id), {"kind": "remove"})
                    removed += 1
                except Exception as exc:  # noqa: BLE001
                    failed.append({"id": str(item_id), "error": str(exc)})
        return {"removed": removed, "failed": failed}

    async def is_running(self, session_id: str) -> bool:
        data = await self.list_sessions()
        for item in data.get("items") or []:
            if str(item.get("sessionId") or "") == session_id:
                return bool(item.get("running"))
        return False

    async def select_model(
        self,
        session_id: str,
        provider: str,
        model: str,
        reasoning_effort: str = "",
    ) -> Dict[str, Any]:
        request: Dict[str, Any] = {"sessionId": session_id, "provider": provider, "model": model}
        if reasoning_effort.strip():
            request["reasoningEffort"] = reasoning_effort.strip()
        return await self.client.rpc("session/selectModel", {"request": request})

    async def follow(self, session_id: str) -> AsyncIterator[Any]:
        """订阅会话实时事件（snapshot + event + assistant-stream）。"""

        args = {
            "request": {
                "address": {"kind": "session", "sessionId": session_id},
                "assistantStream": True,
            }
        }
        async for value in self.client.stream("session/follow", args):
            yield value
