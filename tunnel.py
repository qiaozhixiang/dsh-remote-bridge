"""DSH 公网隧道。

两部分组成：

1. **本机反向代理**：监听 ``127.0.0.1:<port>``，把请求转发到 DSH Web。
   转发时重写 ``Host`` 头并剥掉 ``Origin`` / ``Referer``，这样 DSH 的
   ``dsh-client-connection`` 浏览器信任栅栏（只认 loopback Host）会放行 ——
   否则任何从公网域名进来的 ``/api`` 请求都会被判 403。
2. **cloudflared 快速隧道**：``cloudflared tunnel --url http://127.0.0.1:<port>``，
   从输出里解析 ``https://xxx.trycloudflare.com`` 作为公网地址。

代理同时转发 WebSocket（``/api/remote.mux``），因此在公网打开隧道地址也能
直接用完整的 DSH Web UI，而不只是 REST 接口。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

try:  # aiohttp 是 MaiBot 自带依赖，缺失时插件应优雅降级
    from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
except Exception:  # pragma: no cover - 依赖缺失
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    WSMsgType = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]


HOP_HEADERS = {
    "host",
    "origin",
    "referer",
    "connection",
    "upgrade",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "proxy-authorization",
    "proxy-authenticate",
}

TUNNEL_URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def detect_lan_ip() -> str:
    """尽力探测本机局域网 IPv4；失败返回空串。

    只用于把「内网直连地址」展示给用户，不参与任何鉴权判断。
    """

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("8.8.8.8", 80))
            return str(probe.getsockname()[0] or "")
    except Exception:  # noqa: BLE001 - 探测失败是正常情况
        return ""


def lan_url_for(target: str, host: str = "") -> str:
    """把 target 的主机名换成局域网 IP，端口与路径保持不变。"""

    parts = urlsplit(target)
    if not parts.scheme or not parts.hostname:
        return ""
    ip = host or detect_lan_ip()
    if not ip:
        return ""
    netloc = f"{ip}:{parts.port}" if parts.port else ip
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


ACCESS_DOCUMENT_CANDIDATES = (
    Path.home() / ".dsh" / "remote" / "access.json",
    Path.home() / ".dsh" / "access.json",
)


def read_access_document() -> Dict[str, Any]:
    """读取 DSH「远程访问」配置（``access.json``）。

    该文件由 DSH 的远程访问插件维护，``listen.address`` / ``listen.port``
    就是内网通道真正的监听地址 —— DSH 的 Web 端口（默认 3080）通常只绑
    回环，拿它推导内网地址会得到「端口不通」的假地址。
    """

    for path in ACCESS_DOCUMENT_CANDIDATES:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 文件缺失或损坏都按未配置处理
            continue
        if isinstance(data, dict):
            return data
    return {}


def discover_lan_url(target: str, override: str = "", port_hint: int = 0) -> str:
    """推导内网通道地址：优先配置 > access.json 的监听地址 > 本机 IP + 端口。"""

    override = (override or "").strip().rstrip("/")
    if override:
        return override

    document = read_access_document()
    listen = document.get("listen") if isinstance(document.get("listen"), dict) else {}
    address = str((listen or {}).get("address") or "").strip()
    port = int((listen or {}).get("port") or 0)
    if address and port and address not in {"0.0.0.0", "::", "127.0.0.1", "localhost"}:
        return f"http://{address}:{port}"

    parts = urlsplit(target)
    if not parts.scheme or not parts.hostname:
        return ""
    ip = detect_lan_ip()
    if not ip:
        return ""
    chosen = int(port_hint or 0) or parts.port
    netloc = f"{ip}:{chosen}" if chosen else ip
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))

DEFAULT_TIMEOUT = None
if ClientTimeout is not None:
    DEFAULT_TIMEOUT = ClientTimeout(total=120, sock_connect=15, sock_read=120)


def find_cloudflared(extra_candidates: Optional[List[str]] = None) -> Optional[str]:
    """按优先级查找 cloudflared 可执行文件。"""

    candidates: List[str] = []
    if extra_candidates:
        candidates.extend(extra_candidates)
    env_path = os.environ.get("CLOUDFLARED_PATH", "").strip()
    if env_path:
        candidates.append(env_path)
    home = Path.home()
    candidates.extend(
        [
            str(home / ".dsh" / "remote" / "bin" / "cloudflared.exe"),
            str(home / ".dsh" / "remote" / "bin" / "cloudflared"),
            str(home / "cloudflared.exe"),
        ]
    )
    for name in ("cloudflared", "cloudflared.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    for item in candidates:
        if not item:
            continue
        path = Path(item).expanduser()
        if path.is_file():
            return str(path)
    return None


class TunnelManager:
    """管理反向代理 + cloudflared 子进程。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._runner: Optional[Any] = None
        self._site: Optional[Any] = None
        self._process: Optional[subprocess.Popen] = None
        self._task: Optional[asyncio.Task] = None
        self._public_url: str = ""
        self._local_port: int = 0
        self._state: str = "stopped"
        self._error: str = ""
        self._started_at: float = 0.0
        self._public_reachable: bool = False
        self._reach_task: Optional[asyncio.Task] = None
        self._log: List[str] = []

    # ------------------------------------------------------------ 只读状态

    def status(self) -> Dict[str, Any]:
        cfg = self._plugin._tunnel_config()
        process_alive = self._process is not None and self._process.poll() is None
        running = self._state == "running" and process_alive
        target = (cfg.target_url or self._plugin._connection_config().base_url).rstrip("/")
        lan_shared = discover_lan_url(
            target,
            override=getattr(cfg, "lan_url", "") or "",
            port_hint=int(getattr(cfg, "lan_port", 0) or 0),
        )
        endpoints = {
            "direct": target,
            "lan": lan_shared,
            "public": self._public_url,
            "proxy": f"http://127.0.0.1:{self._local_port}" if self._local_port else "",
        }
        return {
            "state": self._state,
            "running": running,
            "mode": getattr(self._plugin, "_endpoint_mode", "direct"),
            "active_url": self._plugin._get_bridge().client.base_url,
            "public_url": self._public_url,
            "local_url": f"http://127.0.0.1:{self._local_port}" if self._local_port else "",
            "target": cfg.target_url,
            "direct_url": target,
            "lan_url": lan_shared,
            "endpoints": endpoints,
            "public_ready": bool(running and self._public_url),
            "public_reachable": bool(running and self._public_reachable),
            "cloudflared": cfg.cloudflared_path or find_cloudflared() or "",
            "error": self._error,
            "auto_start": cfg.auto_start,
            "log": self._log[-8:],
        }

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> Dict[str, Any]:
        if web is None or ClientSession is None:
            raise RuntimeError("缺少 aiohttp 依赖，无法启动隧道代理")

        cfg = self._plugin._tunnel_config()
        if self._state == "running" and self._process is not None and self._process.poll() is None:
            return self.status()

        await self.stop()

        binary = cfg.cloudflared_path or find_cloudflared() or ""
        if not binary:
            self._state = "error"
            self._error = "未找到 cloudflared，可在插件配置 tunnel.cloudflared_path 指定路径"
            return self.status()

        self._error = ""
        self._log = []
        self._state = "starting"

        target = cfg.target_url or self._plugin._connection_config().base_url
        port = int(cfg.proxy_port or 0)
        try:
            self._runner, self._local_port = await self._start_proxy(target, port)
        except OSError as exc:
            # 端口被上一次没清干净的代理占着时，退到系统分配的空闲端口，
            # 而不是让整个隧道起不来。
            if port:
                self._log.append(f"代理端口 {port} 不可用（{exc}），改用随机端口")
                self._runner, self._local_port = await self._start_proxy(target, 0)
            else:
                raise
        self._log.append(f"代理已启动 {target} -> 127.0.0.1:{self._local_port}")

        try:
            self._process = subprocess.Popen(  # noqa: S603 - 本地可信可执行文件
                [binary, "tunnel", "--url", f"http://127.0.0.1:{self._local_port}", "--no-autoupdate"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
            )
        except Exception as exc:  # noqa: BLE001
            await self.stop()
            self._state = "error"
            self._error = f"cloudflared 启动失败: {exc}"
            return self.status()

        self._public_url = await self._wait_for_url(cfg.startup_timeout_seconds)
        if not self._public_url:
            await self.stop()
            self._state = "error"
            self._error = "cloudflared 未在超时时间内给出公网地址，请查看日志"
            return self.status()

        self._state = "running"
        self._started_at = asyncio.get_event_loop().time()
        self._task = asyncio.create_task(self._watch())
        self._reach_task = asyncio.create_task(self._probe_public())
        self._log.append(f"隧道已就绪 {self._public_url}（等待域名生效）")
        return self.status()

    async def stop(self) -> Dict[str, Any]:
        if self._reach_task is not None:
            self._reach_task.cancel()
            self._reach_task = None
        if self._task is not None:
            self._task.cancel()
            self._task = None
        # 先落状态再收资源：aiohttp 的 cleanup 偶尔会在仍有挂起连接时卡住，
        # 那会让整个 stop() 停在 await 上，外部看到 state 永远是 running。
        self._local_port = 0
        self._public_url = ""
        self._public_reachable = False
        self._state = "stopped"
        process = self._process
        self._process = None
        if process is not None:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(asyncio.to_thread(process.wait, 5), timeout=8)
                except Exception:  # noqa: BLE001
                    process.kill()
            except Exception:  # noqa: BLE001
                pass
        # 先关监听 socket（site.stop 是确定性的），再清 runner。只调 cleanup()
        # 时，若还有 keep-alive 连接挂着，socket 会一直留在监听状态，下次启动
        # 就会撞「端口被占用」。
        site = self._site
        self._site = None
        if site is not None:
            try:
                await asyncio.wait_for(site.stop(), timeout=5)
            except Exception:  # noqa: BLE001
                pass
        runner = self._runner
        self._runner = None
        if runner is not None:
            try:
                await asyncio.wait_for(runner.cleanup(), timeout=6)
            except Exception:  # noqa: BLE001
                pass
        return self.status()

    # -------------------------------------------------------------- 内部

    async def _probe_public(self) -> None:
        """后台探测公网地址是否已经生效，避免阻塞 tunnel_start 调用。"""

        try:
            reachable = await self._wait_until_reachable(self._public_url, 90.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._log.append(f"公网探测异常: {exc}")
            return
        self._public_reachable = reachable
        self._log.append("公网地址已可访问" if reachable else "公网域名 90 秒内仍未生效")

    async def _wait_until_reachable(self, url: str, timeout: float = 25.0) -> bool:
        """轮询公网地址，等 DNS 生效且首页可访问。

        cloudflared 打出 URL 时 DNS 记录往往还没铺开，直接访问会
        ``getaddrinfo failed``；这里用短超时反复重试，避免把「刚建好」
        误判成「不可用」。
        """

        if not url or ClientSession is None:
            return False
        deadline = asyncio.get_event_loop().time() + max(float(timeout), 5.0)
        probe_timeout = ClientTimeout(total=8, sock_connect=5)
        while asyncio.get_event_loop().time() < deadline:
            try:
                async with ClientSession(timeout=probe_timeout) as session:
                    async with session.get(url.rstrip("/") + "/", allow_redirects=False) as response:
                        if response.status < 500:
                            return True
            except Exception:  # noqa: BLE001 - DNS 未生效属预期
                pass
            await asyncio.sleep(2)
        return False

    async def _watch(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        loop = asyncio.get_event_loop()
        while process.poll() is None:
            line = await loop.run_in_executor(None, process.stdout.readline)
            if not line:
                break
            line = line.strip()
            if line:
                self._log.append(line)
                if len(self._log) > 200:
                    del self._log[:100]
        if self._state == "running":
            self._state = "stopped"
            self._error = "cloudflared 已退出"

    async def _wait_for_url(self, timeout: float) -> str:
        process = self._process
        if process is None or process.stdout is None:
            return ""
        loop = asyncio.get_event_loop()

        def read_line() -> str:
            assert process.stdout is not None
            try:
                return process.stdout.readline() or ""
            except Exception:  # noqa: BLE001
                return ""

        deadline = loop.time() + max(5.0, float(timeout or 45))
        while loop.time() < deadline:
            line = await loop.run_in_executor(None, read_line)
            if not line:
                if process.poll() is not None:
                    return ""
                await asyncio.sleep(0.2)
                continue
            self._log.append(line.strip())
            match = TUNNEL_URL_PATTERN.search(line)
            if match:
                return match.group(0)
        return ""

    async def _start_proxy(self, target: str, port: int) -> tuple[Any, int]:
        plugin = self._plugin

        async def handler(request: "web.Request") -> "web.StreamResponse":
            if request.headers.get("Upgrade", "").lower() == "websocket":
                return await self._proxy_ws(request, target)
            return await self._proxy_http(request, target)

        app = web.Application(client_max_size=256 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        try:
            await site.start()
        except Exception:
            await runner.cleanup()
            raise
        self._site = site
        bound = port
        if not bound:
            server = getattr(site, "_server", None)
            sockets = getattr(server, "sockets", None) or []
            if sockets:
                bound = sockets[0].getsockname()[1]
        plugin.ctx.logger.info(f"DSH 隧道代理监听 127.0.0.1:{bound} -> {target}")
        return runner, bound

    async def _proxy_http(self, request: "web.Request", target: str) -> "web.StreamResponse":
        url = target.rstrip("/") + request.rel_url.raw_path_qs
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in HOP_HEADERS and k.lower() != "content-length"
        }
        body = await request.read() if request.can_read_body else None
        # auto_decompress=False：上游若返回 gzip，正文保持压缩态，这样把 Content-Encoding
        # 原样转给浏览器才不会出现「解压后仍声称 gzip」的错乱。
        async with ClientSession(timeout=DEFAULT_TIMEOUT, auto_decompress=False) as session:
            async with session.request(
                request.method,
                url,
                headers=headers,
                data=body,
                allow_redirects=False,
            ) as upstream:
                response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
                for key, value in upstream.headers.items():
                    if key.lower() in HOP_HEADERS or key.lower() == "content-length":
                        continue
                    response.headers[key] = value
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response

    async def _proxy_ws(self, request: "web.Request", target: str) -> "web.StreamResponse":
        server_ws = web.WebSocketResponse(heartbeat=None)
        await server_ws.prepare(request)
        ws_base = target.rstrip("/").replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        try:
            async with ClientSession() as session:
                async with session.ws_connect(ws_base + "/api/remote.mux", heartbeat=None) as client_ws:

                    async def pump_down() -> None:
                        async for message in client_ws:
                            if message.type == WSMsgType.TEXT:
                                await server_ws.send_str(message.data)
                            elif message.type == WSMsgType.BINARY:
                                await server_ws.send_bytes(message.data)
                            else:
                                break
                        await server_ws.close()

                    async def pump_up() -> None:
                        async for message in server_ws:
                            if message.type == WSMsgType.TEXT:
                                await client_ws.send_str(message.data)
                            elif message.type == WSMsgType.BINARY:
                                await client_ws.send_bytes(message.data)
                            else:
                                break
                        await client_ws.close()

                    await asyncio.gather(pump_down(), pump_up(), return_exceptions=True)
        finally:
            if not server_ws.closed:
                await server_ws.close()
        return server_ws
