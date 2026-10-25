"""通用表格 / 文件操作引擎。

把「用户指令」翻译成一组步骤（plan），逐步执行。步骤只做纯计算或文件读写，
发送到对话由插件层负责，因此本模块可以单独测试。

plan 的形状::

    {"title": "按班级统计平均分",
     "steps": [
        {"op": "load", "path": "成绩单.xlsx", "as": "t1"},
        {"op": "group", "input": "t1", "by": ["班级"],
         "agg": [{"col": "成绩", "func": "avg", "as": "平均分"}], "as": "t2"},
        {"op": "save", "input": "t2", "path": "班级平均分.xlsx"},
        {"op": "send", "path": "班级平均分.xlsx"}
     ]}
"""

from __future__ import annotations

import ast
import csv
import importlib.util
import io as _io
import json
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def _load_sibling(name: str) -> Any:
    """加载同目录模块（插件目录不在 sys.path，不能直接 import）。"""

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


office_io = _load_sibling("office_io")
analysis = _load_sibling("analysis")
xlsx_fmt = _load_sibling("xlsx_fmt")


class OpError(Exception):
    """操作无法执行（参数错误 / 文件不存在 / 类型不支持）。"""


# --------------------------------------------------------------------- 数据帧


@dataclass
class Frame:
    """一张可在步骤之间传递的表。"""

    table: "analysis.Table"
    label: str = ""

    @property
    def header(self) -> List[str]:
        return self.table.header

    @property
    def body(self) -> List[List[str]]:
        return self.table.body

    def clone(self, header: Sequence[Any], body: Sequence[Sequence[Any]], label: str = "") -> "Frame":
        rows = [[str(cell) for cell in header]] + [[str(cell) for cell in row] for row in body]
        table = analysis.Table(
            path=self.table.path,
            sheet=self.table.sheet,
            rows=rows,
            header_index=0,
        )
        return Frame(table=table, label=label or self.label)


def _frame_of(header: Sequence[Any], body: Sequence[Sequence[Any]], *, path: Optional[Path] = None,
              sheet: str = "", label: str = "") -> Frame:
    rows = [[str(cell) for cell in header]] + [[str(cell) for cell in row] for row in body]
    return Frame(table=analysis.Table(path=path, sheet=sheet, rows=rows, header_index=0), label=label)


def _norm(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _num(value: Any) -> Optional[float]:
    return analysis.to_number(value)


def _col_index(frame: Frame, name: Any) -> int:
    """把列名 / 列字母 / 1 基序号解析成 0 基列号。"""

    if isinstance(name, int):
        index = name - 1 if name >= 1 else 0
        if 0 <= index < len(frame.header):
            return index
        raise OpError(f"列序号超出范围：{name}")
    text = _norm(name)
    if not text:
        raise OpError("列名不能为空")
    header = frame.header
    for index, title in enumerate(header):
        if title == text:
            return index
    lowered = text.lower()
    for index, title in enumerate(header):
        if title.lower() == lowered:
            return index
    for index, title in enumerate(header):
        if lowered in title.lower():
            return index
    if re.fullmatch(r"[A-Za-z]{1,2}", text):
        index = analysis.col_index(text.upper())
        if 0 <= index < len(header):
            return index
    if text.startswith("#") and text[1:].isdigit():
        index = int(text[1:]) - 1
        if 0 <= index < len(header):
            return index
    raise OpError(f"找不到列：{text}（现有列：{', '.join(header)}）")


def _columns(frame: Frame, names: Any) -> List[int]:
    if names is None or names == "" or names == []:
        return list(range(len(frame.header)))
    if isinstance(names, (str, int)):
        names = [names]
    return [_col_index(frame, item) for item in names]


# --------------------------------------------------------------------- 条件


_COMPARE_OPS = {
    "eq": lambda a, b: _norm(a) == _norm(b),
    "ne": lambda a, b: _norm(a) != _norm(b),
    "gt": lambda a, b: (_num(a) is not None and _num(b) is not None and _num(a) > _num(b)),
    "gte": lambda a, b: (_num(a) is not None and _num(b) is not None and _num(a) >= _num(b)),
    "lt": lambda a, b: (_num(a) is not None and _num(b) is not None and _num(a) < _num(b)),
    "lte": lambda a, b: (_num(a) is not None and _num(b) is not None and _num(a) <= _num(b)),
    "contains": lambda a, b: _norm(b) in _norm(a),
    "not_contains": lambda a, b: _norm(b) not in _norm(a),
    "startswith": lambda a, b: _norm(a).startswith(_norm(b)),
    "endswith": lambda a, b: _norm(a).endswith(_norm(b)),
    "empty": lambda a, b: not _norm(a),
    "not_empty": lambda a, b: bool(_norm(a)),
    "in": lambda a, b: _norm(a) in ([_norm(x) for x in b] if isinstance(b, (list, tuple, set)) else [_norm(b)]),
    "not_in": lambda a, b: _norm(a) not in ([_norm(x) for x in b] if isinstance(b, (list, tuple, set)) else [_norm(b)]),
}

_OP_ALIASES = {
    "=": "eq", "==": "eq", "等于": "eq", "是": "eq",
    "!=": "ne", "<>": "ne", "不等于": "ne",
    ">": "gt", "大于": "gt", ">=": "gte", "大于等于": "gte",
    "<": "lt", "小于": "lt", "<=": "lte", "小于等于": "lte",
    "包含": "contains", "不包含": "not_contains",
    "开头是": "startswith", "结尾是": "endswith",
    "为空": "empty", "非空": "not_empty",
    "in": "in", "属于": "in", "not_in": "not_in", "不属于": "not_in",
}


def _normalize_op(name: Any) -> str:
    text = _norm(name) or "eq"
    return _OP_ALIASES.get(text, text)


def _row_matches(frame: Frame, row: Sequence[Any], where: Sequence[Dict[str, Any]]) -> bool:
    for cond in where or []:
        column = _col_index(frame, cond.get("col"))
        value = row[column] if column < len(row) else ""
        op = _normalize_op(cond.get("op"))
        func = _COMPARE_OPS.get(op)
        if func is None:
            raise OpError(f"不支持的比较方式：{cond.get('op')}")
        if not func(value, cond.get("value")):
            return False
    return True


# --------------------------------------------------------------------- 表达式


_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.USub, ast.UAdd,
    ast.Call, ast.Attribute, ast.Compare, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Eq, ast.NotEq, ast.IfExp, ast.BoolOp, ast.And, ast.Or, ast.Not,
)

_ALLOWED_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max, "int": int, "float": float,
    "len": len, "sum": sum,
}


def _eval_expr(expr: str, values: Dict[str, float]) -> Any:
    """只允许四则运算与少量函数的安全求值。"""

    text = _norm(expr)
    if not text:
        return ""
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise OpError(f"表达式语法错误：{expr}（{exc.msg}）") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise OpError(f"表达式里不允许 {type(node).__name__}：{expr}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                raise OpError(f"表达式里不允许调用该函数：{expr}")
    namespace: Dict[str, Any] = dict(_ALLOWED_FUNCS)
    namespace.update(values)
    try:
        return eval(compile(tree, "<office-expr>", "eval"), {"__builtins__": {}}, namespace)  # noqa: S307
    except Exception as exc:  # noqa: BLE001
        raise OpError(f"表达式计算失败：{expr}（{exc}）") from exc


def _format_number(value: Any) -> str:
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        if abs(value - round(value)) < 1e-9:
            return str(int(round(value)))
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return _norm(value)


# --------------------------------------------------------------------- 表格步骤


def op_select(frame: Frame, columns: Any) -> Frame:
    indexes = _columns(frame, columns)
    header = [frame.header[i] for i in indexes]
    body = [[row[i] if i < len(row) else "" for i in indexes] for row in frame.body]
    return frame.clone(header, body)


def op_filter(frame: Frame, where: Any) -> Frame:
    if isinstance(where, dict):
        where = [where]
    if not where:
        return frame.clone(frame.header, frame.body)
    body = [row for row in frame.body if _row_matches(frame, row, where)]
    return frame.clone(frame.header, body)


def op_sort(frame: Frame, by: Any, desc: bool = False, numeric: bool = True) -> Frame:
    indexes = _columns(frame, by)
    if not indexes:
        return frame.clone(frame.header, frame.body)

    def key(row: Sequence[Any]) -> Tuple:
        parts: List[Any] = []
        for index in indexes:
            value = row[index] if index < len(row) else ""
            number = _num(value) if numeric else None
            parts.append((0, number) if number is not None else (1, _norm(value)))
        return tuple(parts)

    body = sorted(frame.body, key=key, reverse=bool(desc))
    return frame.clone(frame.header, body)


def op_dedupe(frame: Frame, columns: Any = None) -> Frame:
    indexes = _columns(frame, columns)
    seen = set()
    body = []
    for row in frame.body:
        key = tuple(_norm(row[i]) if i < len(row) else "" for i in indexes)
        if key in seen:
            continue
        seen.add(key)
        body.append(row)
    return frame.clone(frame.header, body)


def op_rename(frame: Frame, mapping: Dict[str, str]) -> Frame:
    header = list(frame.header)
    for old, new in (mapping or {}).items():
        index = _col_index(frame, old)
        header[index] = _norm(new) or header[index]
    return frame.clone(header, frame.body)


def op_drop(frame: Frame, columns: Any) -> Frame:
    drop = set(_columns(frame, columns))
    keep = [i for i in range(len(frame.header)) if i not in drop]
    header = [frame.header[i] for i in keep]
    body = [[row[i] if i < len(row) else "" for i in keep] for row in frame.body]
    return frame.clone(header, body)


def op_addcol(frame: Frame, column: str, value: Any = "") -> Frame:
    name = _norm(column) or "新列"
    header = list(frame.header) + [name]
    body = [list(row) + [_norm(value)] for row in frame.body]
    return frame.clone(header, body)


def op_compute(frame: Frame, column: str, expr: str) -> Frame:
    name = _norm(column) or "计算列"
    header = list(frame.header) + [name]
    body = []
    for row in frame.body:
        values: Dict[str, float] = {}
        for index, title in enumerate(frame.header):
            number = _num(row[index] if index < len(row) else "")
            if number is not None and title:
                values[title] = number
                values[re.sub(r"[^\w\u4e00-\u9fff]", "_", title)] = number
        result = _eval_expr(expr, values)
        body.append(list(row) + [_format_number(result)])
    return frame.clone(header, body)


def op_replace(frame: Frame, column: Any = None, find: str = "", to: str = "", regex: bool = False) -> Frame:
    header = list(frame.header)
    indexes = _columns(frame, column)
    pattern = re.compile(find) if regex and find else None
    body = []
    for row in frame.body:
        cells = list(row)
        for index in indexes:
            if index >= len(cells):
                continue
            text = _norm(cells[index])
            if pattern is not None:
                cells[index] = pattern.sub(to, text)
            elif find:
                cells[index] = text.replace(find, to)
        body.append(cells)
    return frame.clone(header, body)


def op_limit(frame: Frame, count: int, from_: str = "head") -> Frame:
    limit = max(0, int(count or 0))
    body = frame.body[-limit:] if _norm(from_).lower() in {"tail", "last", "尾", "末尾"} else frame.body[:limit]
    return frame.clone(frame.header, body)


def op_group(frame: Frame, by: Any, agg: Sequence[Dict[str, Any]]) -> Frame:
    key_indexes = _columns(frame, by)
    specs = list(agg or [])
    if not specs:
        raise OpError("group 步骤缺少 agg 聚合定义")
    agg_indexes = [_col_index(frame, spec.get("col")) for spec in specs]
    header = [frame.header[i] for i in key_indexes] + [
        _norm(spec.get("as")) or f"{_norm(spec.get('func')) or 'sum'}({_norm(spec.get('col'))})" for spec in specs
    ]
    buckets: Dict[Tuple[str, ...], List[Sequence[Any]]] = {}
    order: List[Tuple[str, ...]] = []
    for row in frame.body:
        key = tuple(_norm(row[i]) if i < len(row) else "" for i in key_indexes)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(row)

    body = []
    for key in order:
        rows = buckets[key]
        out = list(key)
        for spec, index in zip(specs, agg_indexes):
            func = _norm(spec.get("func")).lower() or "sum"
            numbers = [n for n in (_num(row[index] if index < len(row) else "") for row in rows) if n is not None]
            texts = [_norm(row[index] if index < len(row) else "") for row in rows]
            if func in {"sum", "求和"}:
                value = _format_number(sum(numbers))
            elif func in {"avg", "average", "mean", "平均"}:
                value = _format_number(sum(numbers) / len(numbers)) if numbers else ""
            elif func in {"count", "计数"}:
                value = str(len(rows))
            elif func in {"counta", "非空计数"}:
                value = str(sum(1 for text in texts if text))
            elif func in {"min", "最小"}:
                value = _format_number(min(numbers)) if numbers else ""
            elif func in {"max", "最大"}:
                value = _format_number(max(numbers)) if numbers else ""
            elif func in {"first", "首个"}:
                value = texts[0] if texts else ""
            elif func in {"last", "末个"}:
                value = texts[-1] if texts else ""
            elif func in {"join", "拼接"}:
                value = _norm(spec.get("sep") or "、").join(text for text in texts if text)
            else:
                raise OpError(f"不支持的聚合方式：{func}")
            out.append(value)
        body.append(out)
    return frame.clone(header, body)


def op_rankcheck(frame: Frame, by: Any = "", rank_cols: Any = None, percent: Any = 30,
                 who: str = "") -> Frame:
    """按分组算名次的百分位，判断是否在组内前 percent%。

    适合「成绩排名」「综合考评排名」这类已经给出名次的表格。
    """

    threshold = float(percent or 30)
    key_idx = _columns(frame, by) if by else []
    key_i = key_idx[0] if key_idx else None

    if rank_cols:
        cols = _columns(frame, rank_cols)
    else:
        cols = [i for i, name in enumerate(frame.header)
                if re.search(r"排名|名次|rank", str(name), re.IGNORECASE)]
    if not cols:
        raise OpError("表格里没有排名列（需要像「成绩排名」「综合考评排名」这样的列）")

    name_i = None
    for hint in ("姓名", "名字", "学生", "name"):
        for index, title in enumerate(frame.header):
            if _norm(title) == hint:
                name_i = index
                break
        if name_i is not None:
            break

    groups: Dict[str, List[List[Any]]] = {}
    for row in frame.body:
        key = _norm(row[key_i]) if key_i is not None and key_i < len(row) else "全部"
        groups.setdefault(key or "全部", []).append(row)

    header = ["分组", "姓名", "项目", "名次", "组内人数", "百分位", f"是否前{_format_number(threshold)}%"]
    body: List[List[Any]] = []
    for key, rows in groups.items():
        total = len(rows)
        for row in rows:
            label = _norm(row[name_i]) if name_i is not None and name_i < len(row) else ""
            if who and who not in " ".join(_norm(cell) for cell in row):
                continue
            for col in cols:
                value = _num(row[col]) if col < len(row) else None
                if value is None or total <= 0:
                    continue
                pct = value / total * 100
                body.append([
                    key, label, str(frame.header[col]), _format_number(value), total,
                    f"{pct:.1f}%", "是" if pct <= threshold else "否",
                ])
    if not body:
        raise OpError("没有可判定的排名数据")
    return _frame_of(header, body, path=frame.table.path, sheet=frame.table.sheet, label="排名核查")


def op_join(left: Frame, right: Frame, on: Any, how: str = "left") -> Frame:
    left_keys = _columns(left, on)
    right_keys = _columns(right, on if not isinstance(on, list) or len(on) != len(left_keys) else on)
    if len(left_keys) != len(right_keys):
        raise OpError("join 的左右关联列数量不一致")
    right_index = {}
    for row in right.body:
        key = tuple(_norm(row[i]) if i < len(row) else "" for i in right_keys)
        right_index.setdefault(key, row)

    right_extra = [i for i in range(len(right.header)) if i not in set(right_keys)]
    header = list(left.header) + [right.header[i] for i in right_extra]
    body = []
    for row in left.body:
        key = tuple(_norm(row[i]) if i < len(row) else "" for i in left_keys)
        match = right_index.get(key)
        if match is None and _norm(how).lower() in {"inner", "内连接"}:
            continue
        extra = [match[i] if match is not None and i < len(match) else "" for i in right_extra]
        body.append(list(row) + extra)
    return left.clone(header, body)


def op_concat(frames: Sequence[Frame]) -> Frame:
    frames = [f for f in frames if f is not None]
    if not frames:
        raise OpError("concat 没有输入")
    header = list(frames[0].header)
    body: List[List[str]] = []
    for frame in frames:
        index_map = []
        for title in header:
            try:
                index_map.append(_col_index(frame, title))
            except OpError:
                index_map.append(-1)
        for row in frame.body:
            body.append([_norm(row[i]) if 0 <= i < len(row) else "" for i in index_map])
    return frames[0].clone(header, body)


def op_transpose(frame: Frame) -> Frame:
    matrix = [frame.header] + [list(row) for row in frame.body]
    if not matrix:
        return frame
    width = max(len(row) for row in matrix)
    padded = [list(row) + [""] * (width - len(row)) for row in matrix]
    transposed = [list(col) for col in zip(*padded)]
    header = transposed[0] if transposed else []
    return frame.clone(header, transposed[1:])


# --------------------------------------------------------------------- 文件步骤


def _read_rows(path: Path, sheet: Optional[str] = None) -> Frame:
    table = analysis.load_table(path, sheet)
    return Frame(table=table, label=path.name)


def save_frame(frame: Frame, path: Path, *, sheet: str = "", fmt: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [frame.header] + [list(row) for row in frame.body]
    suffix = path.suffix.lower()
    if suffix == ".csv":
        buffer = _io.StringIO()
        writer = csv.writer(buffer)
        writer.writerows(rows)
        path.write_text(buffer.getvalue(), encoding="utf-8-sig")
        return {"path": str(path), "rows": len(rows), "kind": "csv"}
    if suffix in {".xlsx", ".xlsm"}:
        options = dict(fmt or {})
        info = xlsx_fmt.write_styled_xlsx(
            path,
            rows,
            sheet_name=_norm(sheet) or (frame.table.sheet if frame.table.sheet and frame.table.sheet != "csv" else "Sheet1"),
            header=bool(options.get("header", True)),
            header_bold=bool(options.get("header_bold", True)),
            freeze_header=bool(options.get("freeze_header", True)),
            autofilter=bool(options.get("autofilter", True)),
            borders=bool(options.get("borders", True)),
            col_widths=options.get("col_widths") or {},
            number_formats=options.get("number_formats") or {},
            align=_norm(options.get("align")) or "left",
            row_height=options.get("row_height"),
        )
        info["kind"] = "xlsx"
        return info
    raise OpError(f"只能保存成 xlsx / csv，收到：{path.suffix or '无扩展名'}")


def replace_in_file(path: Path, find: str, to: str, *, regex: bool = False, count: int = 0) -> Dict[str, Any]:
    """在文本 / docx / xlsx / pptx 里做查找替换。"""

    path = Path(path)
    if not find:
        raise OpError("查找内容不能为空")
    kind = office_io.kind_of(path)
    pattern = re.compile(find) if regex else None

    def _sub(text: str) -> Tuple[str, int]:
        if pattern is not None:
            return pattern.subn(to, text, count=count or 0)
        if count and count > 0:
            return text.replace(find, to, count), text.count(find)
        return text.replace(find, to), text.count(find)

    if kind in {"docx", "xlsx", "pptx", "zip"}:
        changed = 0
        temp = path.with_suffix(path.suffix + ".tmp")
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename.endswith((".xml", ".rels")):
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        dst.writestr(item, data)
                        continue
                    new_text, hits = _sub(text)
                    changed += hits
                    dst.writestr(item, new_text.encode("utf-8"))
                else:
                    dst.writestr(item, data)
        temp.replace(path)
        return {"path": str(path), "replacements": changed, "kind": kind}

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    new_text, hits = _sub(text)
    path.write_text(new_text, encoding="utf-8")
    return {"path": str(path), "replacements": hits, "kind": kind}


def read_text_any(path: Path, limit: int = 4000) -> str:
    return office_io.read_any(Path(path), max_chars=limit)


# --------------------------------------------------------------------- 执行器


class Runtime:
    """执行 plan 时需要的路径能力（由插件提供）。"""

    def __init__(self, resolve: Any, new_path: Any, workspace: Path):
        self.resolve = resolve
        self.new_path = new_path
        self.workspace = Path(workspace)


@dataclass
class PlanResult:
    ok: bool = True
    logs: List[str] = field(default_factory=list)
    previews: List[str] = field(default_factory=list)
    outputs: List[Dict[str, Any]] = field(default_factory=list)
    sends: List[str] = field(default_factory=list)
    error: str = ""

    def log(self, text: str) -> None:
        self.logs.append(text)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def run_plan(plan: Dict[str, Any], runtime: Runtime, *, max_steps: int = 40, allow_delete: bool = False) -> PlanResult:
    """执行 plan，返回执行结果。异常统一转成 ok=False。"""

    result = PlanResult()
    if not isinstance(plan, dict):
        result.ok = False
        result.error = "plan 必须是 JSON 对象"
        return result
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        result.ok = False
        result.error = "plan 里没有 steps"
        return result
    if len(steps) > max_steps:
        result.ok = False
        result.error = f"步骤太多（{len(steps)} > {max_steps}）"
        return result

    frames: Dict[str, Frame] = {}
    paths: Dict[str, Path] = {}

    def _frame(ref: Any) -> Frame:
        if isinstance(ref, Frame):
            return ref
        key = _norm(ref)
        if key in frames:
            return frames[key]
        if key and key in paths:
            return _read_rows(paths[key])
        if not key and frames:
            return list(frames.values())[-1]
        raise OpError(f"找不到数据表引用：{ref}")

    def _path(ref: Any) -> Path:
        key = _norm(ref)
        if key in paths:
            return paths[key]
        return runtime.resolve(key)

    def _target(step: Dict[str, Any], index: int, alias: str) -> str:
        """没有 as 时，把结果写回 input 指向的表（支持 LLM 常见的链式写法）。"""

        if alias:
            return alias
        source = _norm(step.get("input"))
        return source or f"t{index}"

    try:
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                raise OpError(f"第 {index} 步不是对象")
            op = _norm(step.get("op")).lower()
            alias = _norm(step.get("as"))
            if not op:
                raise OpError(f"第 {index} 步缺少 op")

            if op == "load":
                path = runtime.resolve(step.get("path") or step.get("file") or "")
                frame = _read_rows(path, _norm(step.get("sheet")) or None)
                frame.label = path.name
                frames[_target(step, index, alias)] = frame
                paths[alias or f"t{index}"] = path
                result.log(f"读取 {path.name}：{len(frame.body)} 行 × {len(frame.header)} 列")

            elif op == "select":
                frames[_target(step, index, alias)] = op_select(_frame(step.get("input")), step.get("columns"))
            elif op == "filter":
                frames[_target(step, index, alias)] = op_filter(_frame(step.get("input")), step.get("where"))
            elif op == "sort":
                frames[_target(step, index, alias)] = op_sort(
                    _frame(step.get("input")), step.get("by"),
                    bool(step.get("desc", False)), bool(step.get("numeric", True)),
                )
            elif op == "dedupe":
                frames[_target(step, index, alias)] = op_dedupe(_frame(step.get("input")), step.get("columns"))
            elif op == "rename":
                frames[_target(step, index, alias)] = op_rename(_frame(step.get("input")), step.get("mapping") or {})
            elif op == "drop":
                frames[_target(step, index, alias)] = op_drop(_frame(step.get("input")), step.get("columns"))
            elif op in {"addcol", "add_column"}:
                frames[_target(step, index, alias)] = op_addcol(_frame(step.get("input")), step.get("column") or "", step.get("value", ""))
            elif op in {"compute", "computed"}:
                frames[_target(step, index, alias)] = op_compute(_frame(step.get("input")), step.get("column") or "", step.get("expr") or "")
            elif op in {"replace", "replace_values"}:
                frames[_target(step, index, alias)] = op_replace(
                    _frame(step.get("input")), step.get("column"), step.get("find") or "",
                    step.get("to") or "", bool(step.get("regex", False)),
                )
            elif op in {"limit", "head", "tail"}:
                from_ = step.get("from") or ("tail" if op == "tail" else "head")
                frames[_target(step, index, alias)] = op_limit(_frame(step.get("input")), step.get("count") or step.get("n") or 10, from_)
            elif op in {"group", "groupby"}:
                frames[_target(step, index, alias)] = op_group(_frame(step.get("input")), step.get("by"), step.get("agg") or [])
            elif op in {"rankcheck", "rank_check", "percentile"}:
                frames[_target(step, index, alias)] = op_rankcheck(
                    _frame(step.get("input")),
                    step.get("by") or "",
                    step.get("rank_cols") or step.get("columns"),
                    step.get("percent") or step.get("pct") or 30,
                    _norm(step.get("who")),
                )
            elif op == "join":
                frames[_target(step, index, alias)] = op_join(
                    _frame(step.get("left")), _frame(step.get("right")), step.get("on"), _norm(step.get("how")) or "left",
                )
            elif op == "concat":
                frames[_target(step, index, alias)] = op_concat([_frame(item) for item in _as_list(step.get("inputs"))])
            elif op == "transpose":
                frames[_target(step, index, alias)] = op_transpose(_frame(step.get("input")))

            elif op == "preview":
                frame = _frame(step.get("input"))
                limit = int(step.get("limit") or 15)
                lines = [" | ".join(frame.header)]
                for row in frame.body[:limit]:
                    lines.append(" | ".join(_norm(cell) for cell in row))
                if len(frame.body) > limit:
                    lines.append(f"…… 共 {len(frame.body)} 行")
                result.previews.append("\n".join(lines))

            elif op == "save":
                frame = _frame(step.get("input"))
                raw = _norm(step.get("path") or step.get("name"))
                path = runtime.new_path(raw) if raw and not Path(raw).is_absolute() else runtime.resolve(raw)
                info = save_frame(frame, path, sheet=_norm(step.get("sheet")), fmt=step.get("format") or {})
                paths[alias or path.name] = path
                result.outputs.append(info)
                result.log(f"保存 {path.name}：{len(frame.body)} 行")

            elif op == "send":
                path = _path(step.get("path") or step.get("input"))
                result.sends.append(str(path))

            elif op == "list":
                directory = runtime.resolve(step.get("path") or ".")
                keyword = _norm(step.get("keyword"))
                items = []
                for item in sorted(Path(directory).glob("*")):
                    if item.is_file() and (not keyword or keyword in item.name):
                        items.append(f"{item.name}（{office_io.human_size(item.stat().st_size)}）")
                result.previews.append("\n".join(items) or "（目录为空）")

            elif op == "read":
                path = _path(step.get("path") or step.get("input"))
                limit = int(step.get("limit") or 3000)
                result.previews.append(read_text_any(path, limit))

            elif op in {"copy", "move"}:
                src = runtime.resolve(step.get("path") or step.get("from") or "")
                raw = _norm(step.get("to") or "")
                dst = runtime.new_path(raw) if raw and not Path(raw).is_absolute() else runtime.resolve(raw)
                dst.parent.mkdir(parents=True, exist_ok=True)
                if op == "copy":
                    shutil.copy2(src, dst)
                else:
                    shutil.move(str(src), str(dst))
                result.log(f"{'复制' if op == 'copy' else '移动'} {src.name} → {dst.name}")

            elif op == "delete":
                if not allow_delete:
                    raise OpError("当前配置不允许删除文件（behavior.allow_delete=false）")
                path = runtime.resolve(step.get("path") or "")
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                result.log(f"已删除 {path.name}")

            elif op == "mkdir":
                path = runtime.resolve(step.get("path") or "")
                path.mkdir(parents=True, exist_ok=True)
                result.log(f"已创建目录 {path.name}")

            elif op == "replace_in_file":
                path = _path(step.get("path") or step.get("input"))
                info = replace_in_file(path, step.get("find") or "", step.get("to") or "", regex=bool(step.get("regex", False)))
                result.log(f"{path.name} 替换 {info['replacements']} 处")

            elif op == "docx_write":
                raw = _norm(step.get("path") or step.get("name") or "文档.docx")
                path = runtime.new_path(raw)
                office_io.docx_from_markdown(path, step.get("content") or "", title=_norm(step.get("title")))
                result.outputs.append({"path": str(path), "kind": "docx"})
                result.log(f"生成 Word：{path.name}")

            elif op == "pptx_write":
                raw = _norm(step.get("path") or step.get("name") or "演示.pptx")
                path = runtime.new_path(raw)
                office_io.pptx_from_outline(path, _norm(step.get("title")) or "演示文稿", step.get("outline") or "")
                result.outputs.append({"path": str(path), "kind": "pptx"})
                result.log(f"生成 PPT：{path.name}")

            elif op == "zip":
                raw = _norm(step.get("path") or step.get("name") or "打包.zip")
                path = runtime.new_path(raw)
                sources = [runtime.resolve(item) for item in _as_list(step.get("sources") or step.get("inputs"))]
                office_io.create_zip(path, sources)
                result.outputs.append({"path": str(path), "kind": "zip"})
                result.log(f"打包 {len(sources)} 个文件 → {path.name}")

            elif op == "unzip":
                path = _path(step.get("path") or step.get("input"))
                raw = _norm(step.get("to") or "")
                dest = runtime.new_path(raw) if raw else path.with_suffix("")
                office_io.extract_zip(path, dest)
                result.log(f"解压到 {dest.name}")

            elif op == "note":
                result.log(_norm(step.get("text")))

            else:
                raise OpError(f"不支持的步骤：{op}")

        result.logs.insert(0, _norm(plan.get("title")) or "已执行")
        return result
    except (OpError, office_io.OfficeError) as exc:
        result.ok = False
        result.error = str(exc)
        return result
    except FileNotFoundError as exc:
        result.ok = False
        result.error = f"找不到文件：{exc.filename or exc}"
        return result
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        return result


# --------------------------------------------------------------------- 帮助


OP_CATALOG: List[Tuple[str, str, str]] = [
    ("load", "path, sheet?, as?", "读取 xlsx / csv 成数据表"),
    ("select", "input, columns", "只保留指定列"),
    ("filter", "input, where[{col,op,value}]", "按条件筛选行"),
    ("sort", "input, by, desc?, numeric?", "按列排序"),
    ("dedupe", "input, columns?", "按列去重"),
    ("rename", "input, mapping{旧:新}", "重命名列"),
    ("drop", "input, columns", "删除列"),
    ("addcol", "input, column, value", "新增固定值列"),
    ("compute", "input, column, expr", "按表达式计算新列，如 平时*0.3+期末*0.7"),
    ("replace", "input, column?, find, to, regex?", "替换单元格内容"),
    ("limit", "input, count, from?", "取前 N / 后 N 行"),
    ("group", "input, by, agg[{col,func,as}]", "分组聚合 sum/avg/count/min/max/join"),
    ("rankcheck", "input, by?, rank_cols, percent?, who?", "按分组算名次百分位，判断是否在前 N%（如班级前 30%）"),
    ("join", "left, right, on, how?", "按列关联两张表"),
    ("concat", "inputs[]", "纵向合并多张表"),
    ("transpose", "input", "行列转置"),
    ("preview", "input, limit?", "输出文本预览"),
    ("save", "input, path, sheet?, format?", "保存为 xlsx / csv（可带样式）"),
    ("send", "path", "把文件发送到当前对话"),
    ("list", "path?, keyword?", "列出目录文件"),
    ("read", "path, limit?", "读取文件文本"),
    ("copy / move", "path, to", "复制 / 移动文件"),
    ("delete", "path", "删除文件（需 allow_delete）"),
    ("mkdir", "path", "创建目录"),
    ("replace_in_file", "path, find, to, regex?", "在 docx/xlsx/pptx/txt 内查找替换"),
    ("docx_write", "path, content, title?", "按 Markdown 生成 Word"),
    ("pptx_write", "path, title, outline", "按大纲生成 PPT"),
    ("zip / unzip", "path, sources[] / to?", "打包 / 解压"),
]


def catalog_text() -> str:
    lines = ["支持的操作："]
    for name, params, desc in OP_CATALOG:
        lines.append(f"· {name}（{params}）— {desc}")
    return "\n".join(lines)
