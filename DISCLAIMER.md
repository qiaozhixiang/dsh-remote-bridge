# 免责声明与安全须知

本插件会**读写本机文件**并把文件发送到聊天窗口，请在了解风险后再启用。

## 权限与风险

| 能力 | 风险 | 建议 |
| --- | --- | --- |
| 读取文件 | 工作区与 `allowed_roots` 内的文件可被预览、打包、发送 | 用 `[storage] allowed_roots` 限制在专用目录 |
| 写入文件 | 生成 docx/xlsx/pptx/zip，解压 ZIP | 解压已做目录穿越校验，但仍建议只对可信压缩包使用 |
| 删除文件 | `/office rm`、`/office batch delete` 会真删 | 只把管理员加进 `[whitelist] admins` |
| 发送文件 | 通过 `file` 消息段发到当前会话 | 不要把含隐私的目录加进 `allowed_roots` |
| 智能指令 | 模型生成的计划会被自动执行 | `[ai] allow_delete = false`；必要时 `dry_run = true` 先看计划 |
| 聊天文件入库 | 群里别人发的文件会自动落盘 | `[inbox] max_file_mb` 限制大小，定期清理 `data/inbox` |

## 隐私

- 插件不上传任何文件到第三方服务；所有处理都在本机完成。
- 智能指令会把**文件名、表头、行数**拼进提示词发给 MaiBot 配置的大模型；不会发送文件正文。
- 收件箱按 SHA-256 去重，索引保存在插件数据目录的 `inbox/index.json`。

## 白名单

`[whitelist] qq_numbers` 与 `admins` 之外的用户，`/office` 消息会被静默忽略
（不回复、不进入模型）。**默认配置为空 = 所有人都不能用**，请先填自己的 QQ 号。

## 无担保

本插件按 MIT 协议「按原样」提供，不附带任何明示或暗示的担保。
因使用本插件造成的文件损坏、数据丢失、信息泄露等后果由使用者自行承担。

## 合规

请遵守所在平台（QQ / MaiBot 部署方）的服务条款与当地法律法规，
不要用它处理违法内容或他人隐私数据。

## 第三方组件

- [MaiBot](https://github.com/Mai-with-u/MaiBot)：插件宿主，MIT
- Python 标准库：`zipfile`、`xml.etree`、`csv`、`json`、`urllib` 等
