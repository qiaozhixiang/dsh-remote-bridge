# 更新日志

## 1.2.4

### 优化

- `/office` 的全部回复统一做一次清洗：GenUI 卡片围栏摊平成纯文本，JSON/YAML 代码块整块丢弃，XML 标签只留正文，Markdown 标题与加粗去掉。
- `/office ai` 的预览不再输出 `|---|---|` 表格分隔行和多余空行；用户明确要代码或配置时保留原始内容。

## 1.2.3

### 新增

- 聊天文件自动识别：执行 `/office` 命令（含 `/office ai`）前，会先把当前会话最近消息里、以及被回复那条消息里的文件收进收件箱；工作区里没有的同名文件会自动从收件箱复制过来，`/office read 成绩单.xlsx` 这种直接写文件名的用法不再需要先 `get`。
- 指代解析：指令里写「上面的表格 / 刚才发的文件 / 附件」时，自动落到最近收到的那个文件，并在指令末尾补上真实文件名。
- 新增 `rankcheck` 操作：按分组（如班级）计算名次百分位，判断是否在前 N%，输出「分组 / 姓名 / 项目 / 名次 / 组内人数 / 百分位 / 是否前 N%」表格。
- 规则兜底支持「前 30%」这类排名判定指令，不再依赖模型。
- 规则兜底支持「不及格 / 挂科 / 及格 / 优秀」这类不带数字的筛选说法（按 60 / 90 分界）。
- 生成 Excel 时允许用「;」代替换行分隔行，方便在 QQ 里一条消息写完。

### 修复

- `/office ai` 调用宿主大模型固定 30 秒超时（`[E_TIMEOUT] 请求 cap.call 超时`）导致复杂指令直接失败：改为可配置的 `[ai] llm_timeout_seconds`（默认 120 秒），并改用 `call_capability` 直连以传入超时。
- 提示词里的文件清单从 40 个收敛到 12 个，减少模型响应时间。
- 模型调用失败时不再只回一句「没能理解」，现在会先走规则兜底。

## 1.2.2

### 修复

- 修复在 WebUI 配置里把路径连引号一起填写（如 `"<你的工作区目录>"`）导致 `on_load` 抛 `WinError 123`、插件初始化失败并回退旧版本的问题：现在会自动剥掉成对引号，路径不合法时回退到插件数据目录 `files/`，不再让插件加载失败。
- `allowed_roots` 同样会清理引号。
- `config_version` 默认值跟随插件版本，避免 WebUI 保存配置后把版本号写回 1.0.0。

## 1.2.1

### 修复

- **群里 @机器人 时 `/office` 叫不醒**：命令正则原来锚定 `^/office`，而 QQ 适配器会把 @ 转成 `@昵称` 前缀（回复消息还会带 `[reply]`），导致整条命令匹配失败。现在允许前置 `@昵称` / `[reply]` / `[forward]` 与空白，并支持全角 `／office`。
- **白名单为空时静默无响应**：`[whitelist] enabled = true` 且 `qq_numbers` / `admins` 都为空时，命令会直接回一条「白名单是空的，把 <你的QQ> 填进去」的提示，不再让人以为插件坏了。

## 1.2.0

任意自然语言指令 → 操作计划 → 执行。不再局限于固定子命令。

### 新增

- **智能指令引擎**：`/office ai <一句话>`，例如「把成绩单里低于 60 的挑出来发给我」「按班级统计平均分」「把 A.xlsx 按成绩降序，只留姓名和成绩」
  - 优先用宿主 LLM 生成结构化计划（`[ai] model` 留空则用 MaiBot 默认模型）
  - 模型不可用时自动退回关键词规则，常见指令仍可执行
  - `预览 …` / `plan …` 前缀只输出计划不执行；`[ai] dry_run = true` 全局预览
- **操作引擎**（`ops.py`）：load / select / filter / sort / dedupe / rename / drop / addcol / compute / replace / limit / group / join / concat / transpose / preview / save / send / list / read / copy / move / delete / mkdir / replace_in_file / docx_write / pptx_write / zip / unzip / note
  - 条件支持等于/大于/小于/包含/为空/在列表中（含中文别名）
  - 计算列用安全表达式（`成绩*0.6+学分*2`），只允许四则运算与白名单函数
- **API**：`office_ai`、`ai_status`、`ops_catalog`；WebUI 顶部新增「一句话指令」输入框与「预览计划」「支持的操作」按钮
  - 复杂指令超过 8 秒会自动转为后台任务，页面轮询 `aiStatus` 取结果，不会因宿主 10 秒上限报错
- **只在你要求时才发送**：计划里只有出现「发给我 / 发到群里 / 发送」才会加 `send` 步骤
- `[ai]` 配置节：`enabled` / `model` / `temperature` / `max_steps` / `allow_delete` / `dry_run`
- 权限：manifest 新增 `llm.generate`、`message.get_recent` 能力

### 修复

- `/office pptx` 缺参数时误调发送子命令导致异常（改为普通提示）
- 规则兜底在「低于 60」这类指令上猜不到列名（改为按表头匹配数值列）
- 规则兜底把「按班级统计平均分」里的整句当成聚合列（改为分组列与数值列分开猜）

## 1.1.0

聊天文件自动入库 + 文件操作/批处理/差异 + 成绩与奖学金核查 + 表格格式化。

### 新增

- **收件箱**：群聊/私聊收到的文件自动落盘到 `data/inbox`，按 SHA-256 去重，保留发送人、会话、消息 ID；命令 `/office inbox`、`/office sync`、`/office get`、`/office import`
- **差异对比**：`/office diff <A> <B>`，表格逐单元格、文本逐行，报告单元格坐标与列名
- **批处理**：`/office batch <ls|convert|rename|delete|zip> <匹配模式>`，支持通配符
- **单元格/文本编辑**：`/office edit`，xlsx 按 `B2` 定位改值，txt/csv 做替换
- **成绩核查**：`/office check <成绩单> [必修课,…]`，判定挂科、违纪、必修课缺失与平均分
- **奖学金评定**：`/office scholar <成绩单> <要求文件> [排名文件|名次]`，把自然语言要求（平均分/绩点/排名前N/前N%/单科/学分/无挂科/无违纪/必修通过）解析成条款并逐条判定，缺数据显示 ❓
- **表格格式化**：`/office format <目标表> <要求文件>`，支持表头加粗、冻结首行、自动筛选、边框、对齐、列宽、小数位、按列排序、去重、删空行、保留列
- **WebUI**：新增「收件箱」按钮，点击条目即可复制到工作区
- 新增 3 个 API：`inbox_list`、`inbox_get`、`diff_files`

### 说明

- 收件箱文件默认只留在收件箱，用 `/office get` 复制到工作区后再操作；`[inbox] auto_import = true` 可直接落到工作区
- 下载 QQ 文件如需代理，配置 `[inbox] download_proxy`

## 1.0.0

首个版本。

### 新增

- **读取**：Word（docx，段落 / 标题层级 / 表格）、Excel（xlsx，多工作表）、PowerPoint（pptx，逐页文字）、PDF（FlateDecode 文本抽取）、CSV / TXT / MD / JSON、ZIP 条目列表
- **生成**：Word / Excel / PowerPoint，全部用标准库手写 OOXML 包，无第三方依赖
- **打包**：ZIP 创建与解压（含目录穿越防护）
- **投递**：通过 `ctx.send.custom("file", ...)` 发到当前对话，平台不支持时退回文本路径
- **QQ 命令**：`/office` 下 12 个子命令（ls / read / send / docx / xlsx / pptx / zip / unzip / dsh / rm / whoami / help）
- **WebUI 控制台**：文件列表、搜索、预览、生成、发送、删除、DSH 文件导入
- **白名单**：`whitelist.qq_numbers` + `whitelist.admins`，非白名单静默拦截
- **与 dsh-remote-bridge 联动**：通过宿主 `api.call` 调用其 `current_session` 公开 API，直接取走 DSH 会话产物

### 说明

- 敏感目录（`.ssh` / `.gnupg` / `.aws` / `.kube` / `.git` / `*.pem` / `*.key` 等）始终拒绝读写
- 建议把 `storage.allowed_roots` 设为你的工作区目录，进一步缩小可访问范围
