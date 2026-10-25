"""`/office` 的扩展子命令：收件箱、同步、差异、批处理、成绩核查、奖学金、格式化。"""

from __future__ import annotations

import csv
import importlib
import importlib.util
import io as _io
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_MODULE_PREFIX = "maibot_community_office_toolkit"


def _load_sibling(name: str) -> Any:
    """加载同目录模块（插件目录不在 sys.path 里，必须走包名或文件路径）。"""

    module_name = f"{_MODULE_PREFIX}.{name}"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError:
        pass
    path = Path(__file__).resolve().with_name(f"{name}.py")
    if not path.is_file():
        raise ModuleNotFoundError(f"找不到同目录模块：{name}.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"无法加载同目录模块：{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


office_io = _load_sibling("office_io")
analysis = _load_sibling("analysis")
xlsx_fmt = _load_sibling("xlsx_fmt")
textclean = _load_sibling("textclean")
_inbox_module = _load_sibling("inbox")
extract_file_segments = _inbox_module.extract_file_segments
human_size = _inbox_module.human_size

_MAX_LIST = 25


def _split_args(text: str, count: int) -> List[str]:
    """按空格切分，最多 count 段（最后一段保留剩余内容）。"""

    parts = shlex.split(text or "", posix=False)
    if len(parts) <= count:
        return parts
    return parts[: count - 1] + [" ".join(parts[count - 1 :])]


def _fmt_dt(ts: float) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "-"


class OfficeExtraCommandMixin:
    """扩展命令实现，混入 OfficeToolkitPlugin。"""

    # ------------------------------------------------------------ 收件箱
    async def _office_inbox(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del user_id, is_local
        store = self._inbox_store()
        keyword = (rest or "").strip().lower()
        items = store.items()
        if keyword:
            items = [item for item in items if keyword in item.name.lower() or keyword in item.sender_name.lower()]
        if not items:
            await self._office_say(stream_id, "收件箱是空的。群里或私聊发文件给我，我会自动存下来。")
            return
        lines = [f"收件箱（共 {len(items)} 个，最近 {min(len(items), _MAX_LIST)} 个）"]
        for index, item in enumerate(reversed(items[-_MAX_LIST:]), start=1):
            who = item.sender_name or item.sender_id or "未知"
            lines.append(
                f"#{index} {item.name} · {human_size(item.size)} · {who} · {_fmt_dt(item.created_at)}"
            )
        lines.append("")
        lines.append("用法：/office get <#序号> 复制到工作区；/office send 直接发送")
        await self._office_say(stream_id, "\n".join(lines))

    async def _office_sync(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        """把当前会话历史里的文件补收进收件箱。"""

        del user_id, is_local
        limit = 30
        match = re.search(r"\d+", rest or "")
        if match:
            limit = max(5, min(200, int(match.group(0))))
        if not stream_id:
            await self._office_say(stream_id, "拿不到当前会话，无法同步。")
            return
        try:
            messages = await self.ctx.message.get_recent(chat_id=stream_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            await self._office_say(stream_id, f"读取历史消息失败：{exc}")
            return
        if not isinstance(messages, list):
            messages = list(messages or []) if isinstance(messages, (tuple, set)) else []
        store = self._inbox_store()
        added = deduped = failed = 0
        for message in messages:
            for segment in extract_file_segments(message):
                url = segment.get("url") or ""
                if not url:
                    continue
                try:
                    result = store.download(
                        url,
                        name=segment.get("name") or "",
                        sender_id=str((message or {}).get("user_id") or ""),
                        session_id=stream_id,
                        message_id=str((message or {}).get("message_id") or ""),
                        source="history",
                    )
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.debug(f"同步文件失败 {url}: {exc}")
                    failed += 1
                    continue
                if result.get("deduped"):
                    deduped += 1
                else:
                    added += 1
        await self._office_say(
            stream_id,
            f"同步完成：新收 {added} 个，已存在 {deduped} 个，失败 {failed} 个。\n用 /office inbox 查看。",
        )

    async def _office_get(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del user_id, is_local
        store = self._inbox_store()
        item = store.find(rest)
        if item is None:
            await self._office_say(stream_id, f"收件箱里没有：{rest}")
            return
        source = store.path_of(item)
        if not source.is_file():
            await self._office_say(stream_id, f"文件已被清理：{item.rel_path}")
            return
        target = self._new_workspace_path(item.name)
        target.write_bytes(source.read_bytes())
        await self._office_say(stream_id, f"已复制到工作区：{target.name}\n{target}")

    async def _office_import(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del user_id, is_local
        if not rest.strip():
            await self._office_say(stream_id, "用法：/office import <本机文件路径>")
            return
        source = self._resolve_token("", rest.strip())
        store = self._inbox_store()
        result = store.add_path(source, name=source.name, source="local")
        item = result["item"]
        suffix = "（已存在，只更新记录）" if result.get("deduped") else ""
        await self._office_say(stream_id, f"已收进收件箱：{item['name']} · {human_size(item['size'])}{suffix}")

    # ------------------------------------------------------------ 差异
    async def _office_diff(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del user_id, is_local
        parts = _split_args(rest, 2)
        if len(parts) < 2:
            await self._office_say(stream_id, "用法：/office diff <文件A> <文件B>\n支持 xlsx/csv（逐单元格）与 docx/txt（逐行）")
            return
        left = self._resolve_token("", parts[0])
        right = self._resolve_token("", parts[1])
        result = analysis.diff_files(left, right)
        if result.get("mode") == "table":
            lines = [
                f"表格差异：{left.name} ↔ {right.name}",
                f"行数：{result['left']['rows']} → {result['right']['rows']}（新增 {result['added_rows']}，删除 {result['removed_rows']}）",
                f"不同单元格：{result['change_count']}{'（已截断）' if result.get('truncated') else ''}",
            ]
            if result.get("identical"):
                lines.append("两张表完全一致。")
            for change in result.get("changes", [])[:30]:
                lines.append(f"· {change['cell']}（{change['column']}）：{change['left'] or '空'} → {change['right'] or '空'}")
        else:
            lines = [
                f"文本差异：{left.name} ↔ {right.name}",
                f"新增 {result.get('added', 0)} 行，删除 {result.get('removed', 0)} 行",
            ]
            if result.get("identical"):
                lines.append("内容完全一致。")
            body = [line for line in result.get("lines", []) if not line.startswith(("---", "+++"))]
            lines.extend(body[:40])
            if result.get("truncated"):
                lines.append("……（差异过长，已截断）")
        await self._office_say(stream_id, "\n".join(lines))

    # ------------------------------------------------------------ 批处理
    async def _office_batch(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        parts = shlex.split(rest or "", posix=False)
        if not parts:
            await self._office_say(
                stream_id,
                "用法：/office batch <操作> <匹配模式> [参数]\n"
                "操作：ls 列表 / convert 转格式 / rename 重命名 / delete 删除（管理员）/ zip 打包",
            )
            return
        op = parts[0].lower()
        if op == "zip":
            # /office batch zip <输出名> [模式]
            out_name = parts[1] if len(parts) > 1 else "打包.zip"
            pattern = parts[2] if len(parts) > 2 else "*"
        else:
            out_name = ""
            pattern = parts[1] if len(parts) > 1 else "*"
        workspace = self._workspace()
        files = [path for path in sorted(workspace.glob(pattern)) if path.is_file()]
        if not files:
            await self._office_say(stream_id, f"工作区里没有匹配 {pattern} 的文件。")
            return

        if op == "ls":
            lines = [f"匹配 {pattern} 的文件（{len(files)} 个）"]
            for path in files[:40]:
                lines.append(f"· {path.name} · {human_size(path.stat().st_size)}")
            await self._office_say(stream_id, "\n".join(lines))
            return

        if op == "convert":
            target = (parts[2] if len(parts) > 2 else "csv").lower()
            done = []
            for path in files:
                kind = office_io.kind_of(path)
                try:
                    if target == "csv" and kind == "xlsx":
                        table = analysis.load_table(path)
                        out = path.with_suffix(".csv")
                        buffer = _io.StringIO()
                        writer = csv.writer(buffer)
                        writer.writerows([table.header] + table.body)
                        out.write_text(buffer.getvalue(), encoding="utf-8-sig")
                        done.append(out.name)
                    elif target == "txt" and kind in {"docx", "pptx", "pdf"}:
                        text = office_io.read_any(path).get("text") or ""
                        out = path.with_suffix(".txt")
                        out.write_text(text, encoding="utf-8")
                        done.append(out.name)
                except Exception as exc:  # noqa: BLE001
                    done.append(f"{path.name} 失败（{exc}）")
            await self._office_say(stream_id, "转换完成：" + ("、".join(done) if done else "没有可转换的文件"))
            return

        if op == "rename":
            template = parts[2] if len(parts) > 2 else "{name}-{n}{ext}"
            renamed = []
            for index, path in enumerate(files, start=1):
                new_name = (
                    template.replace("{name}", path.stem)
                    .replace("{ext}", path.suffix)
                    .replace("{n}", str(index))
                )
                target = path.with_name(re.sub(r'[<>:"/\\|?*]', "_", new_name))
                path.rename(target)
                renamed.append(target.name)
            await self._office_say(stream_id, "重命名完成：" + "、".join(renamed[:20]))
            return

        if op == "delete":
            if not self._office_is_admin(user_id, is_local):
                await self._office_say(stream_id, "只有管理员能批量删除。")
                return
            names = []
            for path in files:
                names.append(path.name)
                path.unlink(missing_ok=True)
            await self._office_say(stream_id, f"已删除 {len(names)} 个：" + "、".join(names[:20]))
            return

        if op == "zip":
            target = self._new_workspace_path(out_name if out_name.lower().endswith(".zip") else out_name + ".zip")
            office_io.create_zip(target, files, base_dir=workspace)
            await self._office_say(stream_id, f"已打包 {len(files)} 个文件 → {target.name}（{human_size(target.stat().st_size)}）")
            return

        await self._office_say(stream_id, f"不支持的批处理操作：{op}")

    # ------------------------------------------------------------ 成绩核查
    async def _office_check(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del user_id, is_local
        parts = _split_args(rest, 2)
        if not parts:
            await self._office_say(stream_id, "用法：/office check <成绩单.xlsx> [必修课1,必修课2]")
            return
        table = analysis.load_table(self._resolve_token("", parts[0]))
        required = [item for item in re.split(r"[,，、\s]+", parts[1] if len(parts) > 1 else "") if item]
        result = analysis.check_transcript(table, required)
        lines = [f"成绩核查：{table.path.name if table.path else ''}（{result['course_count']} 门课）"]
        lines.append(f"平均分：{result['average'] if result['average'] is not None else '无数据'}｜最低分：{result['min_score']}｜已修学分：{result['total_credit']}")
        lines.append("")
        lines.append(("✅ 无挂科" if not result["failed"] else f"❌ 挂科 {len(result['failed'])} 门：" + "、".join(f"{item['name']}({item['score']})" for item in result["failed"][:12])))
        if result["has_discipline_column"]:
            lines.append("✅ 无违纪记录" if not result["discipline"] else "❌ 违纪：" + "、".join(f"{item['name']}({item['detail']})" for item in result["discipline"][:8]))
        else:
            lines.append("❓ 表里没有违纪/处分列，无法判定")
        if result["has_nature_column"]:
            if result["required_failed"]:
                lines.append(f"❌ 必修课挂科 {len(result['required_failed'])} 门：" + "、".join(item["name"] for item in result["required_failed"][:10]))
            else:
                lines.append(f"✅ 必修课 {result['required_count']} 门全部通过")
        else:
            lines.append("❓ 表里没有课程性质列，无法区分必修")
        if required:
            lines.append("✅ 必修清单齐全" if not result["missing_required"] else "❌ 缺少必修课：" + "、".join(result["missing_required"]))
        lines.append("")
        lines.append("结论：" + ("全部通过 ✅" if result["ok"] else "存在问题 ❌"))
        await self._office_say(stream_id, "\n".join(lines))

    # ------------------------------------------------------------ 奖学金
    async def _office_scholar(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del user_id, is_local
        parts = _split_args(rest, 3)
        if len(parts) < 2:
            await self._office_say(stream_id, "用法：/office scholar <成绩单.xlsx> <奖学金要求文件> [排名文件或名次]")
            return
        table = analysis.load_table(self._resolve_token("", parts[0]))
        requirement_path = self._resolve_token("", parts[1])
        requirement_text = office_io.read_any(requirement_path, max_chars=20000).get("text") or ""
        stats = analysis.compute_stats(table)
        clauses = analysis.parse_requirements(requirement_text)
        if not clauses:
            await self._office_say(stream_id, "没能从要求文件里识别出可判定的条款。请写成「平均分不低于85、无挂科、排名前10%」这类句子。")
            return

        ranking: Optional[Dict[str, Any]] = None
        if len(parts) > 2:
            token = parts[2].strip()
            if token.isdigit():
                ranking = {"rank": int(token), "total": None}
            else:
                rank_path = self._resolve_token("", token)
                rank_table = analysis.load_table(rank_path)
                ranking = analysis.ranking_for_student(rank_table, table)

        results = analysis.evaluate_requirements(clauses, stats, ranking=ranking)
        lines = [f"奖学金评定：{table.path.name if table.path else ''}", f"依据：{requirement_path.name}", ""]
        passed = 0
        for item in results:
            mark = "✅" if item["ok"] else ("❓" if item["ok"] is None else "❌")
            actual = item.get("actual")
            lines.append(f"{mark} {item['label']}（实际：{actual if actual is not None else '无数据'}）")
            if item["ok"]:
                passed += 1
        lines.append("")
        unknown = sum(1 for item in results if item["ok"] is None)
        if all(item["ok"] for item in results):
            lines.append("结论：符合全部硬性条件 ✅")
        elif any(item["ok"] is False for item in results):
            lines.append(f"结论：不符合（{passed}/{len(results)} 条满足，{unknown} 条无法判定）❌")
        else:
            lines.append(f"结论：已满足 {passed}/{len(results)} 条，其余 {unknown} 条缺少数据 ❓")
        await self._office_say(stream_id, "\n".join(lines))

    # ------------------------------------------------------------ 表格格式化
    async def _office_format(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del user_id, is_local
        parts = _split_args(rest, 3)
        if len(parts) < 2:
            await self._office_say(stream_id, "用法：/office format <目标表.xlsx> <要求文件> [输出名]\n要求里可以写：表头加粗、冻结首行、A列宽 15、保留两位小数、按成绩降序、删除重复行、居中等。")
            return
        table = analysis.load_table(self._resolve_token("", parts[0]))
        rules_path = self._resolve_token("", parts[1])
        rules_text = office_io.read_any(rules_path, max_chars=20000).get("text") or ""
        rules = analysis.parse_format_rules(rules_text)
        rows, options = analysis.apply_format_rules(table, rules)
        name = parts[2] if len(parts) > 2 else f"{table.path.stem if table.path else '表格'}-格式化.xlsx"
        if not name.lower().endswith(".xlsx"):
            name += ".xlsx"
        target = self._new_workspace_path(name)
        xlsx_fmt.write_styled_xlsx(target, rows, sheet_name=table.sheet or "Sheet1", **options)
        applied = []
        if rules.get("freeze_header"):
            applied.append("冻结首行")
        if rules.get("header_bold"):
            applied.append("表头加粗")
        if rules.get("autofilter"):
            applied.append("自动筛选")
        if rules.get("borders"):
            applied.append("边框")
        if rules.get("align"):
            applied.append(f"{rules['align']}对齐")
        if rules.get("col_widths"):
            applied.append("列宽")
        if rules.get("number_formats"):
            applied.append("数字格式")
        if rules.get("sort_by"):
            applied.append(f"按{rules['sort_by']['column']}{'降序' if rules['sort_by']['direction'] == 'desc' else '升序'}")
        if rules.get("drop_duplicates"):
            applied.append("去重")
        if rules.get("drop_empty"):
            applied.append("删空行")
        if rules.get("keep_columns"):
            applied.append("保留列 " + ",".join(rules["keep_columns"]))
        await self._office_say(
            stream_id,
            f"已生成：{target.name}\n应用规则：{'、'.join(applied) if applied else '默认样式'}\n共 {len(rows) - 1} 行数据",
        )
        await self._deliver_file(stream_id, target)

    # ------------------------------------------------------------ 编辑
    async def _office_edit(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del user_id, is_local
        parts = _split_args(rest, 3)
        if len(parts) < 3:
            await self._office_say(
                stream_id,
                "用法：\n/office edit <xlsx> <单元格如B2> <新值>\n/office edit <txt/csv> <旧内容> <新内容>",
            )
            return
        path = self._resolve_token("", parts[0])
        kind = office_io.kind_of(path)
        if kind == "xlsx":
            table = analysis.load_table(path)
            cell = parts[1].strip().upper()
            match = re.fullmatch(r"([A-Z]+)(\d+)", cell)
            if not match:
                await self._office_say(stream_id, f"单元格写法不对：{cell}，应形如 B2")
                return
            column = analysis.col_index(match.group(1))
            row_index = int(match.group(2)) - 1
            while len(table.rows) <= row_index:
                table.rows.append([])
            row = table.rows[row_index]
            while len(row) <= column:
                row.append("")
            old = row[column]
            row[column] = parts[2]
            office_io.create_xlsx(path, [{"name": table.sheet, "rows": table.rows}])
            await self._office_say(stream_id, f"已修改 {cell}：{old or '空'} → {parts[2]}")
            return
        text = path.read_text(encoding="utf-8", errors="replace")
        if parts[1] not in text:
            await self._office_say(stream_id, f"没找到要替换的内容：{parts[1]}")
            return
        count = text.count(parts[1])
        path.write_text(text.replace(parts[1], parts[2]), encoding="utf-8")
        await self._office_say(stream_id, f"已替换 {count} 处：{parts[1]} → {parts[2]}")

    # ------------------------------------------------------------ 智能指令

    async def _office_ai(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        """把一句自然语言翻译成操作计划并执行。"""

        del user_id, is_local
        instruction = (rest or "").strip()
        dry_run = False
        match = re.match(r"^(?:预览|看看计划|只预览|dry|plan)\s*[:：]?\s*", instruction, re.IGNORECASE)
        if match:
            dry_run = True
            instruction = instruction[match.end():].strip()
        if not instruction:
            await self._office_say(
                stream_id,
                "用法：/office ai <你想做的事>\n"
                "例：/office ai 把成绩单里不及格的挑出来发给我\n"
                "    /office ai 按班级统计平均分并导出\n"
                "    /office ai 把 成绩单.xlsx 的「成绩」列低于 60 的标出来\n"
                "    /office ai 看看 周报.docx 里写了什么\n"
                "用 /office ops 查看全部可用操作。",
            )
            return

        data = await self.run_instruction(
            instruction, stream_id=stream_id, dry_run=dry_run,
            message=getattr(self, "_office_message", {}) or {},
        )
        if data.get("dry_run"):
            summary = textclean.clean_reply(str(data.get("summary") or ""), allow_code=True, strip_markdown=False)
            await self._office_say(stream_id, f"（预览模式，未执行）\n{summary}")
            return
        if not data.get("ok"):
            message = str(data.get("message") or data.get("error") or "执行失败")
            await self._office_say(stream_id, message)
            return

        allow_code = textclean.wants_code(instruction)
        lines: List[str] = [f"已执行（{data.get('source') or '模型'}解析）"]
        for item in (data.get("logs") or [])[:12]:
            lines.append(f"· {item}")
        for item in (data.get("previews") or [])[:2]:
            text = textclean.clean_reply(str(item), allow_code=allow_code, max_chars=900)
            if not text:
                continue
            lines.append("")
            lines.append(text)
        if data.get("sent"):
            lines.append("")
            lines.append("已发送：" + "、".join(str(name) for name in data["sent"]))
        await self._office_say(stream_id, "\n".join(lines))

    async def _office_ops(self, user_id: str, stream_id: str, rest: str, is_local: bool) -> None:
        del user_id, rest, is_local
        await self._office_say(stream_id, self._ops_catalog_text())

    def _ops_catalog_text(self) -> str:
        module = _load_sibling("ops")
        return module.catalog_text()
