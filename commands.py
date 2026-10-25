"""`/office` QQ 命令层。

命令统一走 ``/office <子命令> <参数>``，白名单与 dsh-remote-bridge 一致：
非白名单用户静默忽略，不进模型。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import Command


def _load_sibling(name: str) -> Any:
    """按文件路径加载插件同目录模块（plugin.py 里同名函数的副本）。"""

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


textclean = _load_sibling("textclean")

# 这些子命令要用到文件，执行前先把当前会话里的文件收进收件箱
_PULL_SUBS = frozenset({
    "read", "send", "diff", "check", "scholar", "format", "edit", "ai", "get", "import", "inbox", "sync",
})

HELP_TEXT = """办公文件工具箱

【怎么指定文件】
· 直接写文件名：/office read 成绩单.xlsx（工作区没有会自动去聊天里找）
· 回复文件再发指令：QQ 里「回复」那条带文件的消息，然后发 /office ai 按班级统计平均分
· 说「上面的表格 / 刚才发的文件」：自动用最近收到的那个文件

【查看】
/office ls [关键字]          列出工作区文件
/office read <路径|#序号>    读取并预览（docx/xlsx/pptx/pdf/csv/txt/zip）
/office send <路径|#序号>    发送到当前对话

【生成】
/office docx <名称> <内容>   生成 Word；用 | 分行，# 标题，- 列表
/office xlsx <名称> <内容>   生成 Excel；用 ; 分行，, 分列
/office pptx <名称> <大纲>   生成 PPT；用 ;; 分页，| 分标题与要点
/office zip <名称> <路径...> 打包成 ZIP
/office unzip <压缩包> [目录] 解压 ZIP

【收件箱（聊天文件自动入库）】
/office inbox [关键字]       查看收件箱（群里/私聊发的文件自动存下来）
/office sync [条数]          把当前会话历史里的文件补收进收件箱
/office get <#序号>          把收件箱文件复制到工作区
/office import <本机路径>    把本机已有文件收进收件箱

【对比与批处理】
/office diff <文件A> <文件B> 逐单元格/逐行对比差异
/office batch <操作> <模式> [参数]
     ls <模式>                 列出匹配文件
     convert <模式> csv|txt    批量转格式
     rename <模式> <模板>      批量重命名（{name} {ext} {n}）
     delete <模式>             批量删除（管理员）
     zip <输出名> [模式]       批量打包
/office edit <xlsx> <B2> <值>  改单元格
/office edit <txt> <旧> <新>   替换文本

【成绩与表格】
/office check <成绩单> [必修课,必修课]   查挂科/违纪/必修
/office scholar <成绩单> <要求文件> [排名文件|名次]
                              逐条比对奖学金要求
/office format <目标表> <要求文件> [输出名]
                              按文字要求格式化表格（表头加粗/冻结/列宽/小数位/排序/去重）

【智能指令（说人话就行）】
/office ai <一句话指令>      自动解析并执行，如「把成绩单里不及格的挑出来发我」
/office ops                  列出智能指令支持的全部操作

【其他】
/office dsh [序号]           查看 DSH 会话里生成的文件（序号=发送）
/office rm <路径|#序号>      删除工作区文件
/office whoami               查看自己的权限
/office help                 显示本帮助

说明：生成的文档默认保存在插件工作区，可用 /office ls 查看。"""


def _split_name(text: str) -> Tuple[str, str]:
    """把 "名称 内容" 拆成两段（名称不含空格时按空格切，否则取第一段）。"""

    stripped = (text or "").strip()
    if not stripped:
        return "", ""
    parts = stripped.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _ensure_suffix(name: str, suffix: str) -> str:
    return name if name.lower().endswith(suffix) else f"{name}{suffix}"


class OfficeCommandMixin:
    """挂在插件类上的 QQ 命令实现。"""

    # ------------------------------------------------------------ 基础

    def _office_cfg(self) -> Any:
        return self._plugin_config().whitelist

    def _office_whitelist(self) -> set:
        numbers = {str(item).strip() for item in (self._office_cfg().qq_numbers or []) if str(item).strip()}
        numbers.update(str(item).strip() for item in (self._office_cfg().admins or []) if str(item).strip())
        numbers.update(str(item).strip() for item in getattr(self, "_office_extra_allow", []) if str(item).strip())
        return numbers

    def _office_admins(self) -> set:
        return {str(item).strip() for item in (self._office_cfg().admins or []) if str(item).strip()}

    def _office_allowed(self, user_id: str, is_local_operator: bool = False) -> bool:
        if is_local_operator:
            return True
        if not getattr(self._office_cfg(), "enabled", True):
            return True
        return str(user_id) in self._office_whitelist()

    def _office_is_admin(self, user_id: str, is_local_operator: bool = False) -> bool:
        if is_local_operator:
            return True
        return str(user_id) in self._office_admins()

    async def _office_say(self, stream_id: str, text: str) -> None:
        payload = textclean.clean_reply(
            text,
            allow_code=True,
            strip_markdown=False,
            keep_empty_note="（这条回复只有结构化内容，已省略）",
        )
        if not payload or not stream_id:
            return
        limit = max(300, int(getattr(self._office_cfg(), "max_reply_chars", 1500) or 1500))
        for index in range(0, len(payload), limit):
            try:
                await self.ctx.send.text(payload[index : index + limit], stream_id)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning(f"发送消息失败: {exc}")
                break

    # ------------------------------------------------------------ 入口

    @Command(
        "office",
        description="办公文件工具箱：读取/生成 Word、Excel、PPT、ZIP，并发送到当前对话",
        pattern=(
            r"^\s*(?:(?:@[^\s/／]+|\[reply\]|\[forward\])\s*)*"
            r"(?:/|／)office(?:\s+(?P<sub>[A-Za-z]+))?(?:\s+(?P<rest>[\s\S]*))?$"
        ),
    )
    async def office_command(self, **kwargs: Any) -> Any:
        user_id = str(kwargs.get("user_id") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "")
        is_local = bool(kwargs.get("is_local_operator"))
        matched = kwargs.get("matched_groups") or {}
        sub = str(matched.get("sub") or "help").strip().lower()
        rest = str(matched.get("rest") or "").strip()
        if stream_id:
            self._office_last_stream = stream_id
        message = kwargs.get("message")
        self._office_message = message if isinstance(message, dict) else {}

        if not self._office_allowed(user_id, is_local):
            if not self._office_whitelist() and getattr(self._office_cfg(), "enabled", True):
                # 白名单开着但一个号码都没填：直接静默会让人以为命令坏了，这里给一次明确提示
                await self._office_say(
                    stream_id,
                    f"办公命令还没启用：白名单是空的。\n"
                    f"把 {user_id} 填进 config.toml 的 [whitelist] qq_numbers，"
                    f"或在 MaiBot WebUI 的插件配置里加到「QQ 白名单」，保存后重试。",
                )
                return True, "白名单为空", 1
            if getattr(self._office_cfg(), "ignore_others", True):
                return True, "已忽略", 1
            await self._office_say(stream_id, "你不在白名单里，无法使用办公文件命令。")
            return True, "无权限", 1

        if sub in _PULL_SUBS and stream_id:
            try:
                await self.pull_chat_files(stream_id, self._office_message)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.debug(f"收取聊天文件失败：{exc}")

        handler = getattr(self, f"_office_{sub}", None)
        if handler is None:
            await self._office_say(stream_id, f"未知子命令：{sub}\n\n{HELP_TEXT}")
            return True, "未知子命令", 1
        try:
            await handler(user_id, stream_id, rest, is_local)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning(f"office 命令 {sub} 执行失败: {exc}")
            await self._office_say(stream_id, f"执行失败：{exc}")
            return False, str(exc), 1
        return True, "ok", 1

    # ------------------------------------------------------------ 子命令

    async def _office_help(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del user_id, rest, is_local
        await self._office_say(stream_id, HELP_TEXT)

    async def _office_whoami(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del rest
        role = "管理员" if self._office_is_admin(user_id, is_local) else "白名单"
        workspace = self._workspace()
        await self._office_say(stream_id, f"你的 QQ：{user_id}\n权限：{role}\n工作区：{workspace}")

    async def _office_ls(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        items = self._list_workspace(rest)
        self._office_list_cache[user_id] = items
        if not items:
            await self._office_say(stream_id, "工作区里还没有文件。用 /office docx|xlsx|pptx 生成一个试试。")
            return
        lines = ["工作区文件："]
        for index, item in enumerate(items[:30], start=1):
            lines.append(f"#{index} {item['name']}（{item['size_text']}）")
        if len(items) > 30:
            lines.append(f"…… 共 {len(items)} 个，只显示前 30 个")
        lines.append("\n用 /office read #序号 预览，/office send #序号 发送。")
        await self._office_say(stream_id, "\n".join(lines))

    async def _office_read(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        path = self._resolve_token(user_id, rest)
        result = self._read_file(path)
        limit = max(300, int(getattr(self._plugin_config().documents, "preview_chars", 1500) or 1500))
        await self._office_say(stream_id, self._preview_text(result, limit))

    async def _office_send(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        path = self._resolve_token(user_id, rest)
        await self._deliver_file(stream_id, path)

    async def _office_docx(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del user_id, is_local
        name, content = _split_name(rest)
        if not name:
            await self._office_say(stream_id, "用法：/office docx <名称> <内容>\n例：/office docx 周报 # 本周进展|- 完成接口联调|- 修复两个线上问题")
            return
        path = self._new_workspace_path(_ensure_suffix(name, ".docx"))
        markdown = content.replace("\\n", "\n").replace("|", "\n")
        info = self._create_docx(path, markdown)
        await self._office_say(stream_id, f"已生成 Word：{path.name}（{info['size']} 字节）\n用 /office send {path.name} 发送到对话。")

    async def _office_xlsx(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del user_id, is_local
        name, content = _split_name(rest)
        if not name:
            await self._office_say(stream_id, "用法：/office xlsx <名称> <内容>\n例：/office xlsx 名单 姓名,年龄;张三,20;李四,30")
            return
        path = self._new_workspace_path(_ensure_suffix(name, ".xlsx"))
        csv_text = content.replace("\\n", "\n").replace(";", "\n")
        info = self._create_xlsx(path, csv_text)
        await self._office_say(stream_id, f"已生成 Excel：{path.name}（{info['sheets']} 个工作表，{info['size']} 字节）")

    async def _office_pptx(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del user_id, is_local
        name, content = _split_name(rest)
        if not name:
            await self._office_say(
                stream_id,
                "用法：/office pptx <名称> <大纲>\n例：/office pptx 季度汇报 封面|第三季度;;进展|完成 A|完成 B;;计划|下一步",
            )
            return
        path = self._new_workspace_path(_ensure_suffix(name, ".pptx"))
        info = self._create_pptx(path, content)
        await self._office_say(stream_id, f"已生成 PPT：{path.name}（{info['slides']} 页，{info['size']} 字节）")

    async def _office_zip(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        name, tail = _split_name(rest)
        if not name:
            await self._office_say(stream_id, "用法：/office zip <名称> <路径或#序号...>")
            return
        tokens = [token for token in tail.replace("，", " ").split() if token]
        if not tokens:
            cached = self._office_list_cache.get(user_id) or []
            if not cached:
                await self._office_say(stream_id, "请给出要压缩的文件，例如：/office zip 打包 #1 #2")
                return
            tokens = [f"#{index}" for index in range(1, len(cached) + 1)]
        paths = [self._resolve_token(user_id, token) for token in tokens]
        target = self._new_workspace_path(_ensure_suffix(name, ".zip"))
        info = self._create_zip(target, paths)
        await self._office_say(stream_id, f"已生成 ZIP：{target.name}（{info['entries']} 个文件，{info['size']} 字节）")

    async def _office_unzip(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        source_token, _, dest_text = (rest or "").partition(" ")
        if not source_token:
            await self._office_say(stream_id, "用法：/office unzip <压缩包路径|#序号> [目标目录]")
            return
        source = self._resolve_token(user_id, source_token)
        if dest_text.strip():
            dest = Path(dest_text.strip()).expanduser()
            if not dest.is_absolute():
                dest = self._workspace() / dest
        else:
            dest = self._workspace() / f"{source.stem}_解压"
        info = self._extract_zip(source, dest)
        await self._office_say(stream_id, f"已解压 {info['entries']} 个文件到：{info['dest']}")

    async def _office_rm(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        path = self._resolve_token(user_id, rest)
        workspace = self._workspace().resolve()
        if workspace not in path.resolve().parents and path.resolve() != workspace:
            await self._office_say(stream_id, "只能删除工作区里的文件。")
            return
        path.unlink()
        await self._office_say(stream_id, f"已删除：{path.name}")

    async def _office_dsh(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del is_local
        files = await self._dsh_files()
        if not files:
            await self._office_say(
                stream_id,
                "没有读到 DSH 会话里的文件。请确认 dsh-remote-bridge 已加载，且会话回复里出现过文件路径。",
            )
            return
        token = (rest or "").strip()
        index = int(token.lstrip("#")) if token.lstrip("#").isdigit() else 0
        if index:
            if index < 1 or index > len(files):
                await self._office_say(stream_id, f"序号超出范围：1~{len(files)}")
                return
            path = Path(str(files[index - 1]["path"]))
            await self._deliver_file(stream_id, path)
            return
        lines = ["DSH 会话生成的文件："]
        for position, item in enumerate(files[:30], start=1):
            lines.append(f"#{position} {item['name']}（{item.get('size_text') or ''}）")
        lines.append("\n用 /office dsh <序号> 直接发送，或 /office read <完整路径> 预览。")
        await self._office_say(stream_id, "\n".join(lines))
