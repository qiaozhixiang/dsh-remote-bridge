## 1.6.1

### 优化

- `/dsh ask` 推回 QQ 的回复先做一次清洗：GenUI 卡片围栏摊平成普通段落，JSON/YAML 代码块整块丢弃，XML 标签只留正文，Markdown 标题、加粗、表格分隔线一并去掉，只保留可读信息。
- 用户明确要代码或配置时（例如「给我一段脚本」「写个 compose 配置」）不做上述裁剪，代码块原样保留。
- `/dsh reply`、`/dsh log` 同样清洗，避免翻历史时把结构代码带回来。

## 1.6.0

### 新增

- 新增「停止当前会话」：`/dsh stop` 现在会中断当前生成、清空排队中的消息（next-turn / next-step）并停止跟随，避免停完又被下一条消息带起来。
- 新增 `session_stop` API（`session_id` 可留空表示当前会话，`clear_queue` 默认 true）。
- WebUI 控制台新增「停止会话」按钮，与「中断」并列；隧道关闭按钮补了二次确认。

### 修复

- 面板里的 `cancel` / `tunnelStop` 动作原来声明了 `confirmation`，而面板不会发送 `confirmed=true`，调用必然 409；已改为在页面里用 `confirm()` 询问。
- `/dsh stop soft` 保留旧行为（只中断当前生成）。

# 更新日志

## 1.5.1

### 修复

- 群里 @机器人 时 `/dsh` 无法触发：命令正则现在允许前置 `@昵称` / `[reply]` / `[forward]` 与空白，并支持全角 `／dsh`。

## 1.5.0

### 新增

- 新增公开 API `current_session`：其他插件可通过 `ctx.api.call("github.qiaozhixiang.dsh-remote-bridge.current_session")` 查询「当前正在对话的 DSH 会话」
- `session_messages` 与 `send_file` 改为 public，其他插件可直接读取会话消息、把文件投递到当前 QQ 对话

### 说明

- 配合 office-toolkit 使用：办公插件生成的 Word/Excel/PPT 可直接发到用户当前对话

## 1.4.0

### 新增

- 文件投递：自动识别助手回复里生成的文件（绝对路径与 `file://` URI），通过 `send.custom` 发送到当前 QQ 对话
- `/dsh files` 列出当前会话最近生成的文件，`/dsh files <序号>` 重发
- WebUI 对话区下方新增「生成的文件」胶囊，点击即发送到当前对话
- 新增 `send_file` API 与 `sendFile` 页面动作
- 新增 `[files]` 配置节：`enabled` / `auto_send` / `max_files` / `max_size_mb` / `allowed_roots`

### 说明

- 只识别「看起来是文件」的绝对路径：必须真实存在、是普通文件、大小不超过上限
- 敏感目录（`.ssh`、`.gnupg`、`.aws`、`.git`、`cookies` 等）始终拒绝
- QQ 侧发送失败时自动降级为文本「文件名：路径」

## 1.3.1

### 修复

- `/dsh ask` 报 `session/model-unavailable: no adapter serves provider "xxx"`：会话里存的 provider 早已被重命名/下线，DSH 找不到适配器就直接拒绝。现在发消息前会自动核对模型目录，发现 provider 不在 `routableProviders` 里就切到默认可用模型再发；发送途中遇到该错误也会自动切换并重试一次（不再把原始报错抛给 QQ）
- 新建会话（`/dsh new`、面板「新建」）会显式绑定当前默认模型，避免继承到已失效的选择
- `/dsh model` 只列出真有适配器的 provider（原来会把已下线的 `xxx` 一起列出来，选了必失败）
- `/dsh model <序号>`、`/dsh ask` 的异常改为一句中文提示，不再暴露 `DshError` 堆栈式文本

## 1.3.0

### 新增

- `/dsh plugins on`（等价 `/dsh plugin on all`）一键开启全部 DSH 插件：后台逐个开启，先回「收到！」，跑完再推汇总（成功/原本已开/只读跳过/失败数），避免 200+ 插件撑爆单次调用等待
- `/dsh lan` 启动内网通道：自动读取 DSH `access.json` 的监听地址（形如 `192.168.x.x:3088`），切过去并验证连通，**只回「已启动」**；`/dsh lan off` 切回直连
- `/dsh tunnel on` 改为**只回「已启动」**，公网地址不再出现在 QQ 消息里（要查地址用 `/dsh tunnel`）

### 修复

- 内网地址推导错误：原来按 `base_url` 端口推导，会得到 `192.168.x.x:3080`（DSH 的 Web 端口只绑回环，根本连不上）。改为读 `access.json` 的 `listen.address`/`listen.port`
- 局域网地址被错误地送进系统代理：`_proxy_for` 原来只跳过回环，`192.168.x.x` 会走 `http_proxy` 导致「连接被拒绝」。现在回环 / RFC1918 / 链路本地 / `.local` / 单段主机名一律直连
- 隧道停止后代理端口仍被占用：`stop()` 现在先 `site.stop()` 关监听 socket 再 `runner.cleanup()`；启动时若端口被占会自动退到随机端口
- 新增 `tunnel.lan_port` 配置项（0 = 自动）

## 1.2.0

### 新增

- 远程对话直接沿用 DSH 同一进程，插件宿主侧能力自动生效；新增 Agent 预设管理：`/dsh preset` 查看、`/dsh preset <名称|序号>` 指定，`/dsh new` 按预设创建会话
- 新增插件管理：`/dsh plugins [关键词]` 列出插件与开关状态，`/dsh plugin on|off <序号|ID>` 开启/关闭插件（走 `pluginManager/setPluginEnabled`，写回 profile 并热应用）
- `/dsh status` 增加「新建会话预设」一行
- README 增加「插件能力（远程对话）」章节

## 1.1.4

### 变更

- `/dsh ask` 收到指令后立即回复「收到！」，不再输出说明性长句
- `/dsh ask` 的最终回复只推送 DSH 的回答原文，去掉「DSH 回复：」前缀与会话/环境信息
- `/dsh reply` 直接输出回答原文，去掉「最近 N 条回复：」标题与 `[seq]` 编号
- `/dsh stop`、`/dsh watch on|off`、`/dsh rename` 成功执行后统一回复「收到！」
- 修复 `/dsh ask` 后台推送把基线写进 stream_id 而不是 QQ 号的问题

## 1.1.3

### 修复

- QQ 侧 `/dsh ask`、`/dsh reply`、`/dsh log` 只取 DSH 回答正文：`assistant` 消息里的 `reasoning`（思考过程）与 `tool-call` 块不再被拼进推送内容
- 额外剥离正文里 `<think>…</think>`、`<reasoning>…</reasoning>` 等包裹段

## 1.1.2

### 新增

- 配置新增 `[connection] http_proxy`：公网隧道地址（`*.trycloudflare.com`）在本机 DNS 被污染时，可指定 HTTP 代理访问；回环地址自动跳过代理
- 支持 `HTTP(S)_PROXY` 之外的手工代理设置，同时覆盖 HTTP 与 WebSocket 两条链路

### 修复

- 切到公网地址时 `getaddrinfo failed` 导致无法连通的问题（无代理环境下仍走直连）

## 1.1.1

### 新增

- **三种连接地址**：`tunnel` 查询新增 `direct_url`（内网本机端口）、`lan_url`（内网同网段设备）、`public_url`（公网隧道）、`endpoints` 汇总
- 新增 `switch_endpoint` API / `switchEndpoint` WebUI 操作：在 `direct`（内网直连）、`lan`（内网共享）、`public`（公网地址）、`custom`（自定义）之间切换 DSH 连接目标
- 配置新增 `[tunnel] lan_url`：手工指定内网共享地址，留空自动推导
- WebUI 隧道面板新增「内网直连 / 内网共享 / 公网地址」三行地址与「切到内网 / 切到公网」按钮

### 变更

- 分发包的 `config.toml` 不含任何白名单号码与个人路径，安装后自行填写

## 1.1.0

### 新增

- **公网隧道**（`tunnel.py`）：本机反向代理（Host 重写）+ `cloudflared` 快速隧道，把 DSH 暴露到公网；支持 `auto_start`、自动探测 cloudflared、启动超时与状态日志
- **QQ 白名单命令层**（`qq_commands.py`）：一个 `/dsh` 命令 + 17 个子命令，可按 QQ 号白名单放行，非白名单静默忽略
- **WebUI 隧道面板**：顶栏「公网隧道」按钮，查看/启停隧道、查看白名单成员
- 新增 WebUI 查询 `tunnel` / `whitelist`，操作 `tunnelStart` / `tunnelStop`
- 配置新增 `[whitelist]` / `[tunnel]` 两节

### 改进

- 会话历史支持向前翻页（`before_seq` + 「加载更早」按钮）
- DSH 响应按 UTF-8 强制解码，修复中文标题乱码

## 1.0.0

首个版本。

### 新增

- DSH 远程连接：`session/list`、`session/page`、`session/projections`、`session/modelCatalog`、`agentPresets/list`
- 实时跟随：基于 `session/follow` 的 WebSocket 长连接 + 游标增量轮询
- 消息操作：发送（`queue` / `steer`）、中断、新建、重命名、切换模型
- 内嵌 WebUI 控制台：单文件自定义页面，会话列表 / 历史渲染 / 推理与工具调用展示 / 模型选择 / 连接自检
- 连接自检：测任意 DSH 地址的连通性与延迟
- 零第三方硬依赖：`aiohttp` → 标准库 `urllib` 自动降级，流式层支持 `aiohttp` / `websockets` 双后端
