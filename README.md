# DSH 远程桥接

把 [DeepSeek Harness（DSH）](https://github.com/deepseek-ai) 的远程会话能力接进 MaiBot，
并在 MaiBot WebUI 里内嵌一个完整可用的 DSH 控制台页面。

## 功能

| 功能 | 说明 |
|---|---|
| 会话列表 | 拉取 DSH 全部会话，显示标题、工作目录、运行状态、最后更新时间，支持关键字搜索 |
| 历史消息 | 按 `throughSeq` 分页读取会话历史，渲染用户消息、助手回复、推理片段、工具调用与工具结果 |
| 实时跟随 | 插件侧维持到 DSH 的 WebSocket 长连接（`session/follow`），页面按游标增量拉取，实时看到生成过程 |
| 发送消息 | 支持 `queue` / `steer` 两种投递模式 |
| 中断生成 | 一键 `session/cancel` |
| 新建 / 重命名会话 | 支持指定工作目录与 Agent 预设 |
| 切换模型 | 从 `session/modelCatalog` 读取真实模型目录，支持推理强度（reasoning effort） |
| 连接自检 | 独立测试任意 DSH 地址的连通性与延迟 |
| 文件投递 | 自动识别回复里生成的文件（绝对路径 / `file://` URI），通过 `send.custom` 发到当前 QQ 对话；面板里也能一键发送 |

## 安装

### 方式一：从 GitHub Release 装（推荐）

1. 打开 [Releases](https://github.com/qiaozhixiang/dsh-remote-bridge/releases)，下载最新版本的
   `Source code (zip)`（GitHub 自动生成的源码包，无需额外构建）。
2. 解压得到 `dsh-remote-bridge-<版本>/` 目录，整个放进 `<MaiBot>/plugins/`。
3. 也可以直接用 MaiBot WebUI → 插件 → 从 ZIP 安装，选择刚下载的 zip。
4. 重启 MaiBot。

> ZIP 根目录必须能直接读到 `_manifest.json`；本仓库的自动源码包顶层只有一层
> `dsh-remote-bridge-<版本>/`，符合 MaiBot 的安装规则。

### 方式二：手工放目录

1. 把整个 `dsh-remote-bridge` 目录放到：

   ```
   <MaiBot>/plugins/dsh-remote-bridge/
   ```

2. 修改 `config.toml`：

   ```toml
   [plugin]
   enabled = true
   config_version = "1.6.2"

   [connection]
   base_url = "http://127.0.0.1:3080"   # DSH Web 地址
   token = ""                            # loopback 直连通常留空
   timeout_seconds = 12.0
   verify_tls = true

   [behavior]
   page_size = 60
   max_sessions = 300
   stream_buffer_size = 600
   allow_write = true                    # 关闭后本插件只能读
   ```

3. 重启 MaiBot，打开 WebUI，左侧栏会出现「DSH 控制台」页面。

## 依赖

插件本身**零第三方依赖**：

* HTTP 层优先用 `aiohttp`，没有则自动降级到标准库 `urllib`
* 流式层优先用 `aiohttp` 的 WebSocket，其次 `websockets`，都没有则实时跟随自动关闭（历史/发送仍可用）

MaiBot 自带的 venv 里两者都有，无需额外安装。

## 配置项说明

| 键 | 默认 | 说明 |
|---|---|---|
| `connection.base_url` | `http://127.0.0.1:3080` | DSH Web 地址，可填 `host:port`（自动补 `http://`） |
| `connection.token` | 空 | DSH 若开了访问令牌校验就填这里 |
| `connection.timeout_seconds` | `12.0` | 单次 HTTP 超时 |
| `connection.verify_tls` | `true` | HTTPS 是否校验证书 |
| `connection.http_proxy` | 空 | 访问公网地址时用的 HTTP 代理（如 `http://127.0.0.1:1080`）；回环地址自动跳过 |
| `behavior.page_size` | `60` | 历史消息一次拉取条数 |
| `behavior.max_sessions` | `300` | 会话列表最多返回条数 |
| `behavior.stream_buffer_size` | `600` | 每会话实时帧环形缓冲上限 |
| `behavior.allow_write` | `true` | 关掉后所有写操作（发送/中断/新建/重命名/切模型）都会被拒绝 |
| `files.enabled` | `true` | 是否识别并投递回复里生成的文件 |
| `files.auto_send` | `true` | `/dsh ask` 收到回复后自动把新生成的文件发到当前对话 |
| `files.max_files` | `5` | 单次最多投递几个文件 |
| `files.max_size_mb` | `25` | 单个文件大小上限（MB） |
| `files.allowed_roots` | `[]` | 只允许投递这些目录下的文件；空表示不限制（敏感目录始终拒绝） |

## 协议说明（给二次开发者）

DSH 0.1.7-rc.1 的远程协议：

**单次调用**

```
POST <base>/api/<namespace>/<method>
Content-Type: application/json

{"type":"client-request","rpcId":"<uuid>","method":"<ns>/<method>","payload":{"args":{...}}}
```

响应：

```json
{"type":"server-response","rpcId":"<uuid>","result":{"ok":true,"value":...}}
{"type":"server-response","rpcId":"<uuid>","result":{"ok":false,"error":{"code":"...","message":"...","details":{}}}}
```

**流式调用**：`ws://<base>/api/remote.mux`

```json
{"type":"open","streamId":"<非空字符串>","endpoint":"session/follow","payload":{"args":{...}}}
```

注意 `streamId` 必须是**字符串**，传数字会被服务端以 `1008 invalid Remote stream request` 断开。

**参数名陷阱**：不同端点的 wire 参数名不同，`session/list` 是 `_request`，其余大多是 `request`。
传错会得到 `gateway/arguments-invalid: missing "_request"`。

## 目录结构

```
dsh-remote-bridge/
├── _manifest.json       插件清单（MaiBot manifest v2）
├── config.toml          插件配置
├── plugin.py            插件主体（配置模型 + @API + 生命周期）
├── qq_commands.py       QQ 命令层（/dsh 系列命令、白名单、回复推送、文件投递）
├── tunnel.py            公网隧道（cloudflared）+ 本地反代
├── files.py             回复中生成文件的识别与发送
├── dsh_client.py        DSH RPC 客户端（HTTP + WebSocket，无第三方硬依赖）
├── webui.json           WebUI 声明（schema_version 2，custom page）
├── webui/
│   └── index.html       内嵌控制台页面（单文件，无外部资源）
├── assets/
│   └── icon.svg         插件图标
├── README.md
└── CHANGELOG.md
```

## 可移植性说明

* 只依赖 `maibot_sdk`，**不 import 任何 `src.*`**（MaiBot 插件硬性要求）
* 不写死任何路径，所有外部状态都在 `config.toml`
* `webui/index.html` 是单文件，无 CDN、无外部字体、无构建步骤
* 页面所有网络请求都经 `window.maibot.query/action` 走宿主桥（iframe CSP 是 `connect-src 'none'`，不能自己 fetch）
* 适配 `host_application 1.3.0 ~ 1.3.99`、`sdk 2.0.0 ~ 2.99.99`

## 已知限制

* 单次 API 调用宿主硬超时 10 秒，所以实时流用「后台长连接 + 页面轮询 1.2s」的方式，不是 SSE
* 页面轮询受宿主令牌桶限制（200 个请求，每 50ms 回补 1 个），因此轮询间隔不低于 1 秒
* DSH 的 `session/search` 在部分部署上是关闭的（`openAt: "never"`），本插件不依赖它，搜索在前端本地做
* 会话里保存的模型可能指向已被改名/下线的 provider（例如 `xxx`）。发消息前插件会自动核对 `session/modelCatalog` 的 `routableProviders`，发现失效就切到默认模型再发；发送途中遇到 `session/model-unavailable` 也会自动切换并重试一次。真修不好时用 `/dsh model` 手动选

## 使用示例

前提：插件已安装并重启 MaiBot，你的 QQ 号在 `[whitelist] qq_numbers` 里（或把 `enabled` 设为 `false` 放开给所有人）。

**1. 看会话、切会话**

```
/dsh ls
/dsh use 3
```

**2. 让 DSH 干活，回复自动推回 QQ**

```
/dsh ask 把 src/utils 下所有函数的 docstring 补全
```

先回「收到！」，随后把 DSH 的回答正文推回来；回复里新生成的文件会自动发到当前对话。

**3. 新建会话并切换模型**

```
/dsh new 重构日志模块
/dsh model
/dsh model 2
```

**4. 开公网隧道（手机也能连）**

```
/dsh tunnel start
```

只回「已启动」，地址不刷屏；需要时用 `/dsh tunnel status` 查。

**5. 批量开关 DSH 插件**

```
/dsh plugins
/dsh plugins on
/dsh plugin off dsh-rewind-plugin
```

## QQ 命令表（/dsh）

白名单内的 QQ 号在群里或私聊发送 `/dsh …` 即可驱动 DSH；非白名单用户的消息会被**静默忽略**（不回复、不进入模型）。

| 命令 | 作用 | 谁能用 |
| --- | --- | --- |
| `/dsh` 或 `/dsh help` | 显示命令表 | 白名单 |
| `/dsh list [n]` | 查看最近 n 个会话（默认 10，最多 30） | 白名单 |
| `/dsh use <序号\|会话ID>` | 切换「当前会话」 | 白名单 |
| `/dsh new [标题]` | 新建会话并自动切过去 | 白名单 |
| `/dsh ask <内容>` | 向当前会话发消息，回复自动推回 | 白名单 |
| `/dsh reply [n]` | 查看最近 n 条 DSH 回复（默认 3） | 白名单 |
| `/dsh log [n]` | 查看最近 n 轮对话（默认 3） | 白名单 |
| `/dsh files` | 列出当前会话回复里生成的文件 | 白名单 |
| `/dsh files <序号>` | 把该文件重发到当前对话 | 白名单 |
| `/dsh stop` | 停止当前会话：中断生成 + 清空排队消息 + 停止跟随 | 白名单 |
| `/dsh stop soft` | 只中断当前生成，保留排队消息 | 白名单 |
| `/dsh watch on\|off` | 自动推送新回复开关 | 白名单 |
| `/dsh rename <标题>` | 重命名当前会话 | 白名单 |
| `/dsh model` | 列出可用模型 | 白名单 |
| `/dsh model <序号> [强度]` | 切换当前会话模型（强度 off/low/high/max） | 白名单 |
| `/dsh preset` | 列出 Agent 预设（决定新会话带哪些插件能力） | 白名单 |
| `/dsh preset <名称\|序号>` | 给之后新建的会话指定预设 | 白名单 |
| `/dsh plugins [关键词]` | 查看 DSH 插件及开关状态 | 白名单 |
| `/dsh plugins on` | **一键开启全部 DSH 插件**（后台执行，先回「收到！」，跑完推汇总） | 白名单 |
| `/dsh plugin on\|off <序号\|ID>` | 开启 / 关闭某个 DSH 插件 | 白名单 |
| `/dsh plugin on all` | 一键开启全部 DSH 插件（同上） | 白名单 |
| `/dsh lan` | **启动内网通道**：自动切到 `access.json` 里的局域网地址并验证，只回「已启动」 | 白名单 |
| `/dsh lan off` | 关闭内网通道，切回直连 | 白名单 |
| `/dsh status` | 桥接状态、当前会话、预设、隧道状态 | 白名单 |
| `/dsh tunnel` | 查看公网隧道状态与地址 | 白名单 |
| `/dsh tunnel on\|off` | 启动 / 停止公网隧道（开启只回「已启动」，地址用 `/dsh tunnel` 查） | 白名单 |
| `/dsh whoami` | 查看自己的 QQ 与权限 | 白名单 |
| `/dsh allow <QQ>` | 把某人加入白名单 | 管理员 |
| `/dsh deny <QQ>` | 把某人移出白名单 | 管理员 |
| `/dsh allowlist` | 查看白名单 | 管理员 |

白名单位于 `config.toml`：

```toml
[whitelist]
enabled = true
qq_numbers = ["<你的QQ号>", "<另一个QQ号>"]
admins = ["<管理员QQ号>"]
ignore_others = true          # 非白名单静默忽略
reply_timeout_seconds = 180   # /dsh ask 等待回复的超时
max_reply_chars = 1200        # 单条推送的最大字符数
```

> `/dsh ask` 把指令交给 DSH 后立刻回「收到！」，之后**只推 DSH 的回答原文**，不带「DSH 回复：」前缀、不带会话 ID / 运行环境等附加信息。
>
> `/dsh ask`、`/dsh reply`、`/dsh log` 只推送 DSH 的**回答正文**：`assistant` 消息里的 `reasoning`（思考过程）与 `tool-call` 块会被过滤掉；正文里 `<think>…</think>` 之类包裹段也会剥掉。WebUI 控制台仍然能看到完整的思考链（作为单独的可折叠块）。
>
> `/dsh watch on|off`、`/dsh rename` 成功执行后同样只回「收到！」；`/dsh stop` 会回一句停止结果。

## 插件能力（远程对话）

远程对话与桌面端用的是同一个 DSH 进程，插件宿主侧能力（工具、命令、模型适配、skill）天然生效，不需要额外配置。

- **预设决定挂载哪些能力**：`/dsh preset` 列出 `standard`、`ptc`、`minimal`、`cordis`、`redteam` 等；选定后 `/dsh new` 建出的会话按该预设挂载工具。`minimal` 最精简，`standard` 是默认全套。
- **插件开关**：`/dsh plugins [关键词]` 看清单（●开启 ○关闭），`/dsh plugin on|off <序号|ID>` 直接开关。开关走 DSH 的 `pluginManager/setPluginEnabled`，会写回当前 profile 并热应用，不用重启 DSH。
- **只影响宿主侧**：标记 `readOnlyReason` 的条目（如 `cordis:include`）不允许远程切换，命令会返回错误。
- **客户端侧 UI 插件**（`dsh-tauri`、`dsh-tauri-ui` 注入的面板）只在桌面壳里渲染，纯浏览器访问隧道时可能看不到，但宿主侧功能照常。

## 公网隧道

`[tunnel]` 段控制：`enabled`（总开关）、`auto_start`（随插件启动）、`cloudflared_path`（留空则自动探测 `~/.dsh/remote/bin/cloudflared.exe` 或 PATH）、`target_url`（留空则用 `[connection] base_url`）、`proxy_port`（本机反向代理端口，默认 3081）、`lan_url` / `lan_port`（内网通道地址与端口，留 0 自动读 `access.json`）。

隧道启动后 `public_url` 即为公网入口；WebUI 顶部「公网隧道」按钮可视化管理，`/dsh tunnel on|off` 也能在 QQ 里开关。快速隧道（trycloudflare）无需账号，但地址每次重启都会变，且没有可用性保证。

首次生成域名后通常要 8–40 秒才真正可解析。如果本机 DNS 无法解析 `*.trycloudflare.com`，在 `[connection] http_proxy` 填上可用代理（例如 `http://127.0.0.1:1080`），插件会在访问公网地址时自动使用它，内网直连仍然走直连。

## 三种连接地址

插件把 DSH 的入口分成三类，面板里分别展示，避免把「内网端口」和「公网域名」混用：

| 场景 | 地址 | 说明 |
|---|---|---|
| 内网 · 本机 | `http://127.0.0.1:3080` | `[connection] base_url`，插件默认走这条 |
| 内网 · 同网段设备 | `http://<局域网IP>:<端口>` | 优先用 `[tunnel] lan_url`；留空则读 DSH `access.json` 的 `listen.address`/`listen.port`（默认 3088）。DSH 的 Web 端口 3080 只绑回环，不能拿来当内网地址 |
| 外网 | `https://xxx.trycloudflare.com` | 隧道启动后生成，只在隧道运行期间有效 |

- 内网直接用端口连，不需要开隧道；
- 外网必须用公网地址，且隧道必须处于 `running`；
- WebUI 面板有「切到内网」「切到公网」两个按钮，等价于把插件的连接目标切到 `direct` / `public`；
- QQ 侧可以用 `/dsh tunnel on` 启动隧道，再用 `/dsh status` 看当前连接地址；
- `/dsh lan` 直接切到内网通道（只回「已启动」），`/dsh lan off` 切回直连。

> 打包分发的 `config.toml` 不包含任何白名单号码与个人路径，安装后请自行填写 `[whitelist] qq_numbers`、`admins`，以及按需填写 `[tunnel] lan_url`。
