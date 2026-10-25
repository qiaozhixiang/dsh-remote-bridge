# 办公文件工具箱（MaiBot 插件）

把 Word / Excel / PowerPoint / PDF / ZIP 的**读取、生成与投递**接进 [MaiBot](https://github.com/Mai-with-u/MaiBot)：
在 QQ 里用 `/office` 命令操作，或在 MaiBot WebUI 的插件扩展页里用图形界面操作。

- **零第三方依赖**：docx / xlsx / pptx 的读写全部用 Python 标准库实现（`zipfile` + `xml.etree`），
  不需要 `python-docx`、`openpyxl`、`python-pptx`、`pypdf`。
- **可移植**：只依赖 `maibot_sdk`，不 import 任何 `src.*`；整个目录拷到别的 MaiBot 1.3.x 实例即可用。
- **可联动**：装了 [dsh-remote-bridge](https://github.com/qiaozhixiang/dsh-remote-bridge) 时，
  可以把 DSH 会话里生成的文件直接取过来发送。

## 功能

| 能力 | 说明 |
| --- | --- |
| 读取 Word | 段落（保留标题层级）、表格、纯文本 |
| 读取 Excel | 多工作表、二维表、共享字符串 / 内联字符串 / 布尔值 |
| 读取 PPT | 每页文字，按页序输出 |
| 读取 PDF | 尽力抽取文本（FlateDecode 内容流；扫描件可能为空） |
| 读取 CSV / TXT / MD / JSON / ZIP | 直接预览，ZIP 列出条目 |
| 生成 Word | Markdown 风格：`#` 标题、`-` 列表、`\| a \| b \|` 表格 |
| 生成 Excel | CSV 风格：`;` 分行、`,` 分列 |
| 生成 PPT | 大纲风格：`;;` 分页、`\|` 分标题与要点 |
| 打包 / 解压 | ZIP 创建与解压（防目录穿越） |
| 投递 | 作为 `file` 消息段发到当前对话；不支持时退回文本路径 |
| 智能指令 | 一句话描述需求，自动生成多步操作计划并执行（LLM 优先，规则兜底） |
| WebUI | 文件列表、预览、生成、发送、删除、一句话指令 |

## 安装

### 方式一：从 ZIP 安装（推荐）

1. 打开 MaiBot WebUI → 插件 → 从 ZIP 安装
2. 选择 `office-toolkit-1.0.0.zip`
3. 重启 MaiBot

### 方式二：手工放目录

把整个 `office-toolkit` 目录放进 `MaiBot/plugins/`，重启 MaiBot。

> 目录里必须有 `_manifest.json` 与 `plugin.py`，且目录名不能以 `.` 开头。

## 使用示例

**1. 直接读聊天里发的文件**

在群里发一个 `成绩单.xlsx`，然后：

```
/office read 成绩单.xlsx
/office ai 把上面的表格里不及格的挑出来
```

**2. 从零生成文件**

```
/office xlsx 课表 姓名,周一,周二;张三,数学,英语;李四,物理,化学
/office docx 会议纪要 时间：周五 14:00；议题：版本发布
```

**3. 一句话批处理**

```
/office ai 把 10 月报表按部门汇总销售额，另存为 汇总.xlsx
/office ai 对比 旧版.docx 和 新版.docx 的差异
```

**4. 把结果发回 QQ**

```
/office send 汇总.xlsx
```

## 命令表

| 命令 | 说明 |
| --- | --- |
| `/office ls [关键字]` | 列出工作区文件（带 `#序号`，供后续引用） |
| `/office read <路径\|#序号>` | 读取并预览文件 |
| `/office send <路径\|#序号>` | 把文件发到当前对话 |
| `/office docx <名称> <内容>` | 生成 Word |
| `/office xlsx <名称> <内容>` | 生成 Excel |
| `/office pptx <名称> <大纲>` | 生成 PPT |
| `/office zip <名称> <路径...>` | 打包成 ZIP |
| `/office unzip <压缩包> [目录]` | 解压 ZIP |
| `/office dsh [序号]` | 列出 / 发送 DSH 会话生成的文件 |
| `/office rm <路径\|#序号>` | 删除工作区文件 |
| `/office inbox [关键字]` | 查看聊天里收到的文件（收件箱） |
| `/office sync [条数]` | 把当前会话历史里的文件补收进收件箱 |
| `/office get <#序号>` | 把收件箱文件复制到工作区 |
| `/office import <本机路径>` | 把本机已有文件收进收件箱 |
| `/office diff <文件A> <文件B>` | 逐单元格 / 逐行对比差异 |
| `/office batch <操作> <模式> [参数]` | 批处理：ls / convert / rename / delete / zip |
| `/office edit <xlsx> <B2> <值>` | 修改表格单元格 |
| `/office edit <txt> <旧> <新>` | 替换文本内容 |
| `/office check <成绩单> [必修课,…]` | 查挂科 / 违纪 / 必修缺失 |
| `/office scholar <成绩单> <要求文件> [排名文件\|名次]` | 按奖学金要求逐条判定 |
| `/office format <目标表> <要求文件> [输出名]` | 按文字要求格式化表格 |
| `/office ai <一句话指令>` | 自然语言操作：自动生成多步计划并执行 |
| （任意子命令）`<文件名>` | 工作区里没有时会自动去当前聊天里找同名文件并收进来 |
| `/office ops` | 列出智能指令支持的全部操作 |
| `/office whoami` | 查看自己的权限 |
| `/office help` | 显示帮助 |

### 怎么指定文件

三种写法都可以，插件会自动把聊天里的文件收进工作区：

```text
/office read 成绩单.xlsx                       # 直接写文件名
/office ai 按班级统计平均分                      # 说「上面的表格/刚才发的文件」也行
（回复那条带文件的消息）/office ai 把不及格的挑出来   # 回复指定文件
```

### 内容语法

```text
/office docx 周报 # 本周进展|- 完成接口联调|- 修复两个线上问题|下周计划
/office xlsx 名单 姓名,年龄;张三,20;李四,30
/office pptx 季度汇报 封面|第三季度;;进展|完成 A|完成 B;;计划|下一步
```

- Word：`|` 或换行分段；`#` / `##` / `###` 标题；`- ` 列表；`| a | b |` 表格
- Excel：`;` 分行，`,` 分列
- PPT：`;;` 分页，每页第一段是标题，其余是要点

## 配置

`config.toml` 各节与插件配置页一一对应。

```toml
[plugin]
enabled = true
config_version = "1.2.0"

[storage]
workspace = ""            # 留空 = 插件数据目录下的 files
allowed_roots = []        # 允许读写的目录；留空 = 不限制（敏感目录始终排除）
max_size_mb = 50

[documents]
preview_chars = 1500
max_rows = 500
max_paragraphs = 800

[whitelist]
enabled = true
qq_numbers = [123456789]  # 允许使用 /office 的 QQ 号
admins = [123456789]
ignore_others = true      # 非白名单静默忽略
max_reply_chars = 1500

[inbox]
enabled = true
auto_capture = true       # 收到文件自动入库
max_items = 2000
max_file_mb = 50
download_proxy = ""       # 下载 QQ 文件如需代理，例如 http://127.0.0.1:1080
auto_import = false       # true 时同时复制一份到工作区

[integration]
dsh_enabled = true
dsh_plugin_id = "github.qiaozhixiang.dsh-remote-bridge"   # 改成你实际安装的 dsh 插件 id

[ai]
enabled = true
model = ""                # 留空 = 用 MaiBot 默认对话模型
temperature = 0.2
max_steps = 12            # 单条指令最多几步操作
llm_timeout_seconds = 120.0   # 调用大模型最长等待（复杂指令给到 90~180）
allow_delete = false      # 是否允许 AI 计划里删除文件
dry_run = false           # true = 只解析计划不执行
```

### 白名单

`whitelist.qq_numbers` 与 `whitelist.admins` 里的 QQ 号才能使用 `/office`；
其余人的 `/office` 消息被**静默拦截**（不回复、不进入模型）。

## 智能指令

不想记子命令时，直接用一句话描述要做的事：

```text
/office ai 把成绩单里低于 60 的挑出来发给我
/office ai 按班级统计平均分并导出
/office ai 把 成绩单.xlsx 按成绩降序，只保留姓名、课程、成绩
/office ai 看看 周报.docx 里写了什么
/office ai 预览 按班级统计平均分        # 只看计划，不执行
/office ai 根据成绩排名和综合考评排名，看是不是在班级前 30%
/office ai 把 成绩单.xlsx 里低于 60 的挑出来发给我   # 文件只在聊天里也行
/office ops                             # 查看支持的全部操作
```

执行流程：

1. 扫描工作区文件，给每个表格附上表头与行数，拼成提示词
2. 调用宿主 LLM 生成 JSON 计划（`steps` 数组，最多 `[ai] max_steps` 步）
3. 校验计划（步骤数、操作名、参数结构），不合法就丢弃
4. 用 `ops.py` 逐步执行，遇到错误立即停止并回报
5. 计划里的 `send` 步骤会把产物发到当前对话

模型不可用（未配置 / 调用失败 / 返回非 JSON）时自动退回关键词规则，
覆盖筛选、排序、分组统计、重命名、发送、读取、列目录等常见指令。

安全约束：

- 只允许操作工作区与 `[storage] allowed_roots` 里的文件
- `[ai] allow_delete = false` 时，计划里的删除步骤会被拒绝
- 计算列表达式走 AST 白名单，只允许四则运算与 `abs/round/min/max/int/float/len/sum`
- 计划最多 `[ai] max_steps` 步，单步失败即中止

## 与 dsh-remote-bridge 联动

装了 `dsh-remote-bridge` 后：

```text
/office dsh          # 列出 DSH 当前会话里生成的文件
/office dsh 2        # 直接把第 2 个文件发到当前对话
```

实现方式：本插件通过宿主 `api.call` 调用 `dsh-remote-bridge` 的
`current_session` 公开 API，拿到会话 ID 与最近产物文件列表，不重复实现 DSH 协议。

## 目录结构

```text
office-toolkit/
├── _manifest.json       插件清单（manifest v2）
├── config.toml          默认配置
├── plugin.py            插件主体：配置模型 + API + 生命周期
├── commands.py          /office 命令层
├── commands_extra.py    收件箱/差异/批处理/成绩/格式化/智能指令子命令
├── ops.py               通用表格/文件操作引擎（计划执行器）
├── agent.py             自然语言 → 操作计划（LLM + 规则兜底）
├── inbox.py             聊天文件收件箱（落盘 + 去重）
├── analysis.py          表格分析：成绩核查、奖学金、差异、格式规则
├── xlsx_fmt.py          带样式的 xlsx 写出
├── office_io.py         docx / xlsx / pptx / pdf / zip 读写引擎（纯标准库）
├── files.py             路径解析、安全校验、文件消息负载
├── webui.json           WebUI 扩展声明
├── webui/
│   └── index.html       单文件控制台（内联 CSS + JS）
├── assets/
│   └── icon.svg         插件图标
├── README.md
└── CHANGELOG.md
```

## 可移植性

- 仅使用 Python 3.10+ 标准库与 `maibot_sdk`
- 同目录模块通过带前缀的模块名加载（`maibot_community_office_toolkit.*`），避免与其他插件重名
- 不写死任何本机路径、QQ 号或令牌；`config.toml` 里全是占位值

## 安全

- `files.py` 始终拒绝 `.ssh`、`.gnupg`、`.aws`、`.kube`、`.git`、浏览器凭据文件、`*.pem` / `*.key` / `*.pfx` 等敏感路径
- 设置 `storage.allowed_roots` 可进一步把可读写范围限制在工作区
- `/office rm` 只能删除工作区内的文件
- 解压 ZIP 时校验目标路径，阻止 `../` 目录穿越

## 已知限制

- PDF 文本抽取是尽力而为：只处理 FlateDecode 与未压缩内容流，扫描件需要 OCR
- 生成的 PPT 采用最小 OOXML 包，主题为内置默认样式
- `.doc` / `.xls` / `.ppt`（97-2003 二进制格式）不支持，请另存为新格式
- 文件消息依赖平台适配器支持 `file` 消息段；不支持时自动退回发送路径文本

## 开源协议

MIT
