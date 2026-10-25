"""自然语言指令 → 操作计划（plan）→ 执行。

优先用宿主 LLM 把「一句话指令」翻译成 ops.py 的步骤；LLM 不可用或输出不合法时，
退回一套基于关键词的规则解析，保证离线也能干活。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _load_sibling(name: str) -> Any:
    module_name = f"maibot_community_office_toolkit.{name}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"无法加载 {name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


ops = _load_sibling("ops")
office_io = _load_sibling("office_io")
analysis = _load_sibling("analysis")


SYSTEM_PROMPT = """你是「办公文件工具箱」的指令解析器。用户会用中文（或中英混合）描述要对文件做的事。
你要把它翻译成一个 JSON 操作计划，只输出 JSON，不要解释、不要代码围栏。

JSON 结构：
{
  "title": "一句话说明要做什么",
  "steps": [ {"op": "...", ...}, ... ]
}

可用 op（括号内是参数）：
- load(path, sheet?, as?)：读取 xlsx/csv 成数据表，as 是变量名，默认 t1
- select(input, columns)：只保留这些列
- filter(input, where)：where 是条件数组 [{"col":"成绩","op":"lt","value":60}]，
  op 可用 eq/ne/gt/gte/lt/lte/contains/not_contains/startswith/endswith/empty/not_empty/in/not_in
- sort(input, by, desc?, numeric?)：按列排序
- dedupe(input, columns?)：去重
- rename(input, mapping)：{"旧列名":"新列名"}
- drop(input, columns)：删除列
- addcol(input, column, value)：加固定值列
- compute(input, column, expr)：用表达式算新列，expr 里可直接写列名，如 "平时*0.3+期末*0.7"
- replace(input, column?, find, to, regex?)：替换单元格内容
- limit(input, count, from?)：取前 N（from=head）或后 N（from=tail）
- group(input, by, agg)：by 是分组列数组，agg 是 [{"col":"成绩","func":"avg","as":"平均分"}]，
  func 可用 sum/avg/count/counta/min/max/first/last/join
- join(left, right, on, how?)：按列关联，how 默认 left
- concat(inputs)：纵向合并多张表
- transpose(input)：行列转置
- preview(input, limit?)：输出文本预览
- save(input, path, sheet?, format?)：保存为 xlsx/csv；format 可写
  {"header_bold":true,"freeze_header":true,"autofilter":true,"col_widths":{"A":12},
   "number_formats":{"C":"0.00"},"align":"left"}
- send(path)：把文件发到当前对话
- list(path?, keyword?)：列目录
- read(path, limit?)：读文件文本
- copy(path, to) / move(path, to)：复制 / 移动
- delete(path)：删除（仅在允许时）
- mkdir(path)：建目录
- replace_in_file(path, find, to, regex?)：在 docx/xlsx/pptx/txt 里查找替换
- docx_write(path, content, title?)：按 Markdown 生成 Word（content 用 # 标题、- 列表、| 表格）
- pptx_write(path, title, outline)：按大纲生成 PPT（;; 分页，| 分标题与要点）
- zip(path, sources) / unzip(path, to?)
- note(text)：输出一行说明

规则：
1. 只能引用「当前文件清单」里存在的文件；输出文件名要用清单外的名字。
2. 用户说「这个表 / 这份成绩单」时，选清单里最匹配的一个。
3. 涉及统计、筛选、排序时先 load 再 filter/sort/group，最后 save。
4. **只有用户明确说「发给我 / 发到群里 / 发送」时才加 send 步骤**，否则不要发。
5. 不要臆造数据；缺列时用最接近的列名。
6. steps 最多 12 步。
7. 只输出 JSON。
"""


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抠出第一个完整 JSON 对象。"""

    raw = (text or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    depth = 0
    start = -1
    for index, char in enumerate(raw):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                chunk = raw[start : index + 1]
                try:
                    data = json.loads(chunk)
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError:
                    start = -1
    return None


def validate_plan(plan: Any, *, max_steps: int = 12) -> Tuple[bool, str]:
    if not isinstance(plan, dict):
        return False, "计划不是 JSON 对象"
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return False, "计划里没有 steps"
    if len(steps) > max_steps:
        return False, f"步骤太多（{len(steps)} > {max_steps}）"
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            return False, f"第 {index} 步不是对象"
        if not str(step.get("op") or "").strip():
            return False, f"第 {index} 步缺少 op"
    return True, ""


def describe_files(entries: Sequence[Dict[str, Any]], *, limit: int = 12) -> str:
    """把文件清单压成提示词里的短文本。"""

    lines: List[str] = []
    for item in list(entries)[:limit]:
        name = str(item.get("name") or "")
        if not name:
            continue
        extra = []
        kind = item.get("kind")
        if kind:
            extra.append(str(kind))
        size = item.get("size_text")
        if size:
            extra.append(str(size))
        header = item.get("header")
        if header:
            extra.append("列：" + "/".join(str(col) for col in header[:12]))
        rows = item.get("rows")
        if rows:
            extra.append(f"{rows} 行")
        lines.append(f"- {name}" + (f"（{'，'.join(extra)}）" if extra else ""))
    return "\n".join(lines) or "（空）"


def build_prompt(instruction: str, files_text: str, *, workspace: str = "") -> str:
    parts = [
        SYSTEM_PROMPT,
        "\n当前文件清单：",
        files_text,
    ]
    if workspace:
        parts.append(f"\n工作区：{workspace}")
    parts.append(f"\n用户指令：{instruction}")
    parts.append("\n请输出 JSON 计划：")
    return "\n".join(parts)


# --------------------------------------------------------------------- 规则兜底

_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _pick_file(instruction: str, names: Sequence[str]) -> Optional[str]:
    """在指令里找最像文件名的那个。"""

    lowered = instruction.lower()
    best = None
    best_len = 0
    for name in names:
        stem = Path(name).stem.lower()
        if name.lower() in lowered or (stem and stem in lowered):
            if len(name) > best_len:
                best, best_len = name, len(name)
    if best:
        return best
    quoted = re.findall(r"[《\"'“”]([^》\"'“”]+)[》\"'“”]", instruction)
    for item in quoted:
        for name in names:
            if item in name:
                return name
    # 没点名文件时，避开之前生成的中间产物，优先原始数据表
    noise = re.compile(r"结果|汇总|统计|筛选|格式化|副本|output|out|copy", re.IGNORECASE)
    for name in names:
        if not noise.search(Path(name).stem):
            return name
    return names[0] if names else None


def _wants_send(text: str) -> bool:
    """用户是否明确要求把文件发出去。"""

    return bool(re.search(r"发送|发给我|发到|发一下|传给我|给我发|推送", text or ""))


def _number_after(text: str, keywords: Sequence[str]) -> Optional[float]:
    for keyword in keywords:
        match = re.search(re.escape(keyword) + r"[^\d\-]{0,6}(-?\d+(?:\.\d+)?)", text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


def plan_with_rules(instruction: str, names: Sequence[str],
                    headers: Optional[Dict[str, Sequence[str]]] = None) -> Optional[Dict[str, Any]]:
    """没有 LLM 时的关键词兜底，覆盖最常见的几类指令。

    headers 是「文件名 -> 表头列名」，用来把「低于 60」这类指令落到正确的列上。
    """

    text = (instruction or "").strip()
    if not text:
        return None
    target = _pick_file(text, names)
    columns = [str(item) for item in ((headers or {}).get(target or "") or [])]
    steps: List[Dict[str, Any]] = []
    title = text[:40]

    def _load(path: Optional[str] = None) -> str:
        steps.append({"op": "load", "path": path or target or "", "as": "t1"})
        return "t1"

    if re.search(r"删除|删掉|移除文件", text) and target and not re.search(r"删除列|删掉列|删除行", text):
        return {"title": title, "steps": [{"op": "delete", "path": target}]}

    match_pct = re.search(r"前\s*(\d+(?:\.\d+)?)\s*[%％]", text)
    if match_pct and target and re.search(r"排名|名次|百分|前", text):
        rank_cols = [col for col in columns if re.search(r"排名|名次", col)]
        if rank_cols:
            _load()
            by_col = next((col for col in columns if re.search(r"班级|班别|专业|年级|班级名称", col)), "")
            steps.append({
                "op": "rankcheck", "input": "t1", "as": "t2",
                "by": by_col, "rank_cols": rank_cols,
                "percent": float(match_pct.group(1)),
            })
            steps.append({"op": "preview", "input": "t2", "limit": 30})
            steps.append({"op": "save", "input": "t2", "path": "排名核查.xlsx"})
            return {"title": title, "steps": steps}

    if re.search(r"对比|比较|差异|diff", text):
        pair = [name for name in names if Path(name).stem.lower() in text.lower()]
        if len(pair) < 2:
            pair = list(names[:2])
        return {
            "title": title,
            "steps": [
                {"op": "note", "text": "文件差异请用 /office diff 查看逐单元格结果"},
                {"op": "read", "path": pair[0], "limit": 1200},
                {"op": "read", "path": pair[1], "limit": 1200},
            ],
        }

    if re.search(r"筛选|过滤|找出|挑出|只保留", text) and target:
        source = _load()
        where: List[Dict[str, Any]] = []
        # 「不及格 / 挂科 / 及格 / 优秀」这类说法不带数字，单独兜一层
        keyword_filter: Optional[tuple] = None
        if re.search(r"不及格|挂科|没及格|未通过|不通过", text):
            keyword_filter = ("lt", 60.0)
        elif re.search(r"优秀", text):
            keyword_filter = ("gte", 90.0)
        elif re.search(r"及格|通过", text):
            keyword_filter = ("gte", 60.0)
        if keyword_filter is not None:
            where.append({
                "col": _guess_value_column(text, columns, prefer=("成绩", "分数", "得分", "总分")),
                "op": keyword_filter[0],
                "value": keyword_filter[1],
            })
        for op_name, keywords in (
            ("gte", ("大于等于", "不低于", "至少")),
            ("lte", ("小于等于", "不高于", "最多")),
            ("gt", ("大于", "超过", "高于")),
            ("lt", ("小于", "低于", "不足")),
            ("ne", ("不等于",)),
            ("eq", ("等于", "是")),
        ):
            if where:
                break
            value = _number_after(text, keywords)
            if value is not None:
                column = _guess_value_column(text, columns)
                where.append({"col": column, "op": op_name, "value": value})
                break
        if not where:
            match = re.search(r"(?:包含|含有|带有)[“\"']?([^，。,\s”\"']+)", text)
            if match:
                where.append({"col": _guess_column(text, prefer=(), columns=columns),
                              "op": "contains", "value": match.group(1)})
        if where:
            steps.append({"op": "filter", "input": source, "where": where, "as": "t2"})
            steps.append({"op": "preview", "input": "t2", "limit": 12})
            steps.append({"op": "save", "input": "t2", "path": "筛选结果.xlsx"})
            if _wants_send(text):
                steps.append({"op": "send", "path": "筛选结果.xlsx"})
            return {"title": title, "steps": steps}

    if re.search(r"排序|排列|从大到小|从小到大|升序|降序", text) and target:
        source = _load()
        desc = bool(re.search(r"降序|从大到小|倒序", text))
        column = _guess_value_column(text, columns, prefer=("成绩", "分数", "金额", "数量", "排名"))
        steps.append({"op": "sort", "input": source, "by": [column], "desc": desc, "as": "t2"})
        steps.append({"op": "preview", "input": "t2", "limit": 12})
        steps.append({"op": "save", "input": "t2", "path": "排序结果.xlsx"})
        if _wants_send(text):
            steps.append({"op": "send", "path": "排序结果.xlsx"})
        return {"title": title, "steps": steps}

    if re.search(r"平均|求和|合计|统计|汇总|分组", text) and target:
        source = _load()
        by = _guess_group_column(text, columns, prefer=("班级", "部门", "类别", "科目", "课程"))
        agg_col = _guess_value_column(text, columns, prefer=("成绩", "分数", "金额", "数量"))
        func = "avg" if re.search(r"平均", text) else ("count" if re.search(r"计数|多少", text) else "sum")
        steps.append({
            "op": "group", "input": source, "by": [by],
            "agg": [{"col": agg_col, "func": func, "as": f"{func}({agg_col})"}], "as": "t2",
        })
        steps.append({"op": "preview", "input": "t2", "limit": 12})
        steps.append({"op": "save", "input": "t2", "path": "统计结果.xlsx"})
        if _wants_send(text):
            steps.append({"op": "send", "path": "统计结果.xlsx"})
        return {"title": title, "steps": steps}

    if re.search(r"重命名|改列名|列名改", text) and target:
        match = re.search(r"把[“\"']?([^，。,\s”\"']+)[”\"']?改成[“\"']?([^，。,\s”\"']+)", text)
        if match:
            source = _load()
            steps.append({"op": "rename", "input": source, "mapping": {match.group(1): match.group(2)}, "as": "t2"})
            steps.append({"op": "save", "input": "t2", "path": "改名结果.xlsx"})
            if _wants_send(text):
                steps.append({"op": "send", "path": "改名结果.xlsx"})
            return {"title": title, "steps": steps}

    if re.search(r"发送|发给我|传给我", text) and target:
        return {"title": title, "steps": [{"op": "send", "path": target}]}

    if re.search(r"读取|看看|预览|内容", text) and target:
        return {"title": title, "steps": [{"op": "read", "path": target, "limit": 2000}]}

    if re.search(r"有哪些文件|列出|文件列表", text):
        return {"title": title, "steps": [{"op": "list", "path": "."}]}

    return None


_VALUE_HINTS = ("成绩", "分数", "得分", "总分", "平均分", "金额", "数量", "人数",
                "排名", "学分", "年龄", "价格", "绩点")
_NUMERIC_NAME_RE = re.compile(r"成绩|分数|得分|总分|平均分|金额|数量|人数|排名|学分|年龄|价格|绩点")
_NOISE_RE = re.compile(r"统计|平均|求和|汇总|排序|筛选|分组|对比|计算|所有|全部")


def _guess_column(text: str, prefer: Sequence[str] = (), columns: Sequence[str] = ()) -> str:
    """从指令里猜列名：先按表头精确匹配，再退回关键词与「按X」。"""

    for name in sorted(columns, key=lambda item: len(str(item)), reverse=True):
        if str(name) and str(name) in text:
            return str(name)
    for name in prefer:
        if name in text:
            return name
    quoted = re.findall(r"[“\"']([^”\"']{1,12})[”\"']", text)
    for item in quoted:
        return item
    match = re.search(r"按([^\s，。,:：]{1,6})", text)
    if match and not _NOISE_RE.search(match.group(1)):
        return match.group(1)
    if prefer:
        return prefer[0]
    return str(columns[0]) if columns else ""


def _guess_value_column(text: str, columns: Sequence[str] = (), prefer: Sequence[str] = ()) -> str:
    """猜「数值列」，用于筛选阈值、排序、聚合，避免把「班级」当成聚合列。"""

    pool = [str(item) for item in columns if _NUMERIC_NAME_RE.search(str(item))]
    if not pool:
        pool = [str(item) for item in columns]
    for name in list(prefer) + list(_VALUE_HINTS):
        if not name or name not in text:
            continue
        if name in pool:
            return name
        if not columns:
            return name
    if pool:
        return pool[0]
    return prefer[0] if prefer else ""


def _guess_group_column(text: str, columns: Sequence[str] = (), prefer: Sequence[str] = ()) -> str:
    """猜分组列：优先表头里出现的名字，其次 prefer 关键词。"""

    for name in sorted(columns, key=lambda item: len(str(item)), reverse=True):
        if str(name) and str(name) in text:
            return str(name)
    for name in prefer:
        if name in text:
            return name
    match = re.search(r"按([^\s，。,:：]{1,6})", text)
    if match and not _NOISE_RE.search(match.group(1)):
        return match.group(1)
    if prefer:
        return prefer[0]
    return str(columns[0]) if columns else ""


# --------------------------------------------------------------------- LLM 计划


async def plan_with_llm(llm: Any, instruction: str, files_text: str, *, workspace: str = "",
                        model: str = "", temperature: Optional[float] = None,
                        max_steps: int = 12) -> Tuple[Optional[Dict[str, Any]], str]:
    """调用宿主 LLM 生成计划，返回 (plan, 说明)。

    ``llm`` 可以是能力对象（有 ``generate``），也可以直接是一个
    ``async def(prompt) -> dict`` 的调用函数——插件用它给 LLM 更长的超时。
    """

    if llm is None:
        return None, "未接入 LLM"
    prompt = build_prompt(instruction, files_text, workspace=workspace)
    try:
        if callable(llm) and not hasattr(llm, "generate"):
            result = await llm(prompt)
        else:
            result = await llm.generate(prompt, model=model, temperature=temperature, max_tokens=1200)
    except Exception as exc:  # noqa: BLE001
        return None, f"调用模型失败：{exc}"
    if not isinstance(result, dict):
        return None, "模型返回格式异常"
    if result.get("success") is False:
        return None, str(result.get("reason") or "模型调用失败")
    text = str(result.get("response") or result.get("content") or "")
    plan = _extract_json(text)
    if plan is None:
        return None, "模型没有返回合法 JSON"
    ok, message = validate_plan(plan, max_steps=max_steps)
    if not ok:
        return None, message
    return plan, ""


def summarize_plan(plan: Dict[str, Any], *, limit: int = 12) -> str:
    steps = plan.get("steps") or []
    lines = [f"计划：{plan.get('title') or '（未命名）'}"]
    for index, step in enumerate(steps[:limit], start=1):
        op = step.get("op")
        detail = {k: v for k, v in step.items() if k not in {"op", "as"}}
        text = json.dumps(detail, ensure_ascii=False)
        if len(text) > 120:
            text = text[:117] + "…"
        lines.append(f"{index}. {op} {text}")
    if len(steps) > limit:
        lines.append(f"…… 共 {len(steps)} 步")
    return "\n".join(lines)


def file_hints(path: Path) -> Dict[str, Any]:
    """给提示词用的文件摘要（表格带表头，其它只给类型大小）。"""

    info: Dict[str, Any] = {"name": path.name, "kind": office_io.kind_of(path), "size_text": office_io.human_size(path.stat().st_size)}
    try:
        if info["kind"] in {"xlsx", "csv"}:
            table = analysis.load_table(path)
            info["header"] = table.header[:12]
            info["rows"] = len(table.body)
    except Exception:  # noqa: BLE001
        pass
    return info
