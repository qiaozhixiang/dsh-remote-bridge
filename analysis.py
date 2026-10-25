"""表格分析与批处理：成绩单核查、奖学金评定、文件差异、格式规则。

全部基于 office_io 读出的二维字符串表，不依赖第三方库。
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def _load_office_io() -> Any:
    """加载同目录的 office_io（插件目录不在 sys.path，不能直接 import）。"""

    module_name = "maibot_community_office_toolkit.office_io"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().with_name("office_io.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError("无法加载 office_io.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


office_io = _load_office_io()

FAIL_WORDS = {"不及格", "不合格", "未通过", "挂科", "fail", "f", "e", "d-"}
OK_WORDS = {"", "无", "否", "正常", "无违纪", "无处分", "-", "—", "/", "none", "no"}

_HEADER_HINTS = ("课程", "科目", "成绩", "学分", "学号", "姓名", "性质", "类型", "绩点", "排名")
_NATURE_HINTS = ("性质", "类型", "属性", "类别")
_SCORE_HINTS = ("成绩", "分数", "得分", "总评", "综合")
_CREDIT_HINTS = ("学分",)
_COURSE_HINTS = ("课程", "科目", "名称")
_GPA_HINTS = ("绩点", "gpa")
_DISCIPLINE_HINTS = ("违纪", "处分", "纪律", "作弊")
_RANK_HINTS = ("排名", "名次")
_ID_HINTS = ("学号", "编号")
_NAME_HINTS = ("姓名", "名字")


# --------------------------------------------------------------------- 基础
def to_number(value: Any) -> Optional[float]:
    """把单元格转成数字；失败返回 None。"""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace(",", "").replace("，", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def col_letter(index: int) -> str:
    letters = ""
    value = index + 1
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def col_index(letter: str) -> int:
    index = 0
    for char in str(letter).upper():
        if "A" <= char <= "Z":
            index = index * 26 + (ord(char) - 64)
    return index - 1


@dataclass
class Table:
    """一张二维表（含表头定位）。"""

    path: Optional[Path]
    sheet: str
    rows: List[List[str]]
    header_index: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def header(self) -> List[str]:
        if 0 <= self.header_index < len(self.rows):
            return [str(cell).strip() for cell in self.rows[self.header_index]]
        return []

    @property
    def body(self) -> List[List[str]]:
        return self.rows[self.header_index + 1 :]

    def column_of(self, hints: Sequence[str]) -> int:
        """按表头关键字找列号，找不到返回 -1。"""

        for index, title in enumerate(self.header):
            lowered = title.lower()
            if any(hint in lowered for hint in hints):
                return index
        return -1

    def roles(self) -> Dict[str, int]:
        """识别课程/成绩/学分/性质/违纪等列。"""

        mapping: Dict[str, int] = {}
        for index, title in enumerate(self.header):
            lowered = title.lower().strip()
            if not lowered:
                continue
            if "nature" not in mapping and any(h in lowered for h in _NATURE_HINTS):
                mapping["nature"] = index
            elif "credit" not in mapping and any(h in lowered for h in _CREDIT_HINTS):
                mapping["credit"] = index
            elif "score" not in mapping and any(h in lowered for h in _SCORE_HINTS):
                mapping["score"] = index
            elif "gpa" not in mapping and any(h in lowered for h in _GPA_HINTS):
                mapping["gpa"] = index
            elif "discipline" not in mapping and any(h in lowered for h in _DISCIPLINE_HINTS):
                mapping["discipline"] = index
            elif "rank" not in mapping and any(h in lowered for h in _RANK_HINTS):
                mapping["rank"] = index
            elif "sid" not in mapping and any(h in lowered for h in _ID_HINTS):
                mapping["sid"] = index
            elif "name" not in mapping and any(h in lowered for h in _NAME_HINTS):
                mapping["name"] = index
            elif "course" not in mapping and any(h in lowered for h in _COURSE_HINTS):
                mapping["course"] = index
        return mapping


def detect_header(rows: Sequence[Sequence[Any]]) -> int:
    """在前 12 行里挑最像表头的一行。"""

    best_index, best_score = 0, -1.0
    for index, row in enumerate(rows[:12]):
        cells = [str(cell).strip() for cell in row]
        filled = sum(1 for cell in cells if cell)
        if filled < 2:
            continue
        hits = sum(1 for cell in cells if any(hint in cell for hint in _HEADER_HINTS))
        score = hits * 3 + filled
        if score > best_score:
            best_index, best_score = index, score
    return best_index


def load_table(path: Path, sheet: Optional[str] = None) -> Table:
    """读取 xlsx/csv 成 Table。"""

    path = Path(path)
    kind = office_io.kind_of(path)
    if kind == "xlsx":
        result = office_io.read_xlsx(path)
        sheets = result.get("sheets") or []
        if not sheets:
            raise office_io.OfficeError("工作簿里没有工作表")
        chosen = sheets[0]
        if sheet:
            for candidate in sheets:
                if candidate["name"] == sheet:
                    chosen = candidate
                    break
        rows = [[str(cell) for cell in row] for row in chosen["rows"]]
        return Table(path=path, sheet=chosen["name"], rows=rows, header_index=detect_header(rows))
    if kind == "csv":
        import csv
        import io as _io

        text = path.read_text(encoding="utf-8-sig", errors="replace")
        rows = [[str(cell) for cell in row] for row in csv.reader(_io.StringIO(text)) if row]
        return Table(path=path, sheet="csv", rows=rows, header_index=detect_header(rows))
    raise office_io.OfficeError("只支持 xlsx / csv 表格")


# --------------------------------------------------------------------- 成绩单
def course_records(table: Table) -> List[Dict[str, Any]]:
    """把表格解析成课程记录列表。"""

    roles = table.roles()
    course_col = roles.get("course", -1)
    score_col = roles.get("score", -1)
    credit_col = roles.get("credit", -1)
    nature_col = roles.get("nature", -1)
    discipline_col = roles.get("discipline", -1)

    records: List[Dict[str, Any]] = []
    for row in table.body:
        def cell(index: int) -> str:
            return str(row[index]).strip() if 0 <= index < len(row) else ""

        name = cell(course_col)
        score_text = cell(score_col)
        if not name and not score_text:
            continue
        score = to_number(score_text) if score_col >= 0 else None
        lowered = score_text.lower()
        passed: Optional[bool]
        if score is not None:
            passed = score >= 60
        elif lowered in FAIL_WORDS:
            passed = False
        elif lowered in OK_WORDS:
            passed = None
        else:
            passed = None
        credit = to_number(cell(credit_col)) if credit_col >= 0 else None
        records.append(
            {
                "name": name or "（未命名）",
                "score": score,
                "score_text": score_text,
                "passed": passed,
                "credit": credit or 0.0,
                "nature": cell(nature_col),
                "discipline": cell(discipline_col),
            }
        )
    return records


def compute_stats(table: Table) -> Dict[str, Any]:
    """汇总平均分、挂科、违纪、学分等指标。"""

    records = course_records(table)
    scored = [record for record in records if record["score"] is not None]
    credits = [record["credit"] for record in records]
    use_weight = any(value > 0 for value in credits)
    if scored and use_weight:
        weight_sum = sum(record["credit"] or 0.0 for record in scored)
        if weight_sum > 0:
            average = sum((record["score"] or 0.0) * (record["credit"] or 0.0) for record in scored) / weight_sum
        else:
            average = sum(record["score"] or 0.0 for record in scored) / len(scored)
    elif scored:
        average = sum(record["score"] or 0.0 for record in scored) / len(scored)
    else:
        average = None

    failed = [record for record in records if record["passed"] is False]
    required = [record for record in records if "必修" in record["nature"]]
    required_failed = [record for record in required if record["passed"] is False]
    discipline_hits = [
        record
        for record in records
        if record["discipline"] and record["discipline"].lower() not in OK_WORDS
    ]
    passed_credit = sum(record["credit"] for record in records if record["passed"] is not False)
    gpa_values = []
    gpa_col = table.roles().get("gpa", -1)
    if gpa_col >= 0:
        for row in table.body:
            if gpa_col < len(row):
                value = to_number(row[gpa_col])
                if value is not None:
                    gpa_values.append(value)

    return {
        "records": records,
        "course_count": len(records),
        "average": round(average, 2) if average is not None else None,
        "min_score": min((record["score"] for record in scored), default=None),
        "max_score": max((record["score"] for record in scored), default=None),
        "failed": failed,
        "required": required,
        "required_failed": required_failed,
        "discipline": discipline_hits,
        "total_credit": round(passed_credit, 2),
        "gpa": round(sum(gpa_values) / len(gpa_values), 3) if gpa_values else None,
        "weighted": use_weight,
        "columns": table.roles(),
    }


def check_transcript(table: Table, required_courses: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """核查挂科、违纪、必修科目。"""

    stats = compute_stats(table)
    records = stats["records"]
    names = {record["name"].strip() for record in records}

    missing_required: List[str] = []
    for course in required_courses or []:
        cleaned = str(course).strip()
        if cleaned and not any(cleaned in name or name in cleaned for name in names):
            missing_required.append(cleaned)

    passed = stats["course_count"] - len(stats["failed"])
    return {
        "ok": not stats["failed"] and not stats["discipline"] and not missing_required,
        "course_count": stats["course_count"],
        "passed_count": passed,
        "average": stats["average"],
        "min_score": stats["min_score"],
        "failed": [{"name": r["name"], "score": r["score_text"] or r["score"], "credit": r["credit"]} for r in stats["failed"]],
        "required_count": len(stats["required"]),
        "required_failed": [{"name": r["name"], "score": r["score_text"] or r["score"]} for r in stats["required_failed"]],
        "discipline": [{"name": r["name"], "detail": r["discipline"]} for r in stats["discipline"]],
        "missing_required": missing_required,
        "total_credit": stats["total_credit"],
        "gpa": stats["gpa"],
        "has_nature_column": "nature" in stats["columns"],
        "has_discipline_column": "discipline" in stats["columns"],
    }


# --------------------------------------------------------------------- 奖学金
_CLAUSE_PATTERNS: List[Tuple[str, str, str]] = [
    ("avg", r"(?:平均分|平均成绩|均分|加权平均)[^。；;\n]{0,12}?(?:不低于|不少于|达到|至少|≥|>=|高于|超过)\s*(\d+(?:\.\d+)?)", "平均分不低于 {}"),
    ("gpa", r"(?:绩点|GPA)[^。；;\n]{0,12}?(?:不低于|不少于|达到|至少|≥|>=)\s*(\d+(?:\.\d+)?)", "绩点不低于 {}"),
    ("rank_n", r"排名[^。；;\n]{0,10}?(?:前|不超过|≤|<=|进入)\s*(\d+)\s*(?:名|位|以内)", "排名前 {} 名"),
    ("rank_pct", r"排名[^。；;\n]{0,10}?前\s*(\d+(?:\.\d+)?)\s*%", "排名前 {}%"),
    ("min_score", r"(?:单科|任一科|每科|最低分)[^。；;\n]{0,10}?(?:不低于|≥|>=)\s*(\d+(?:\.\d+)?)", "单科不低于 {}"),
    ("credit", r"(?:总学分|学分)[^。；;\n]{0,10}?(?:不低于|≥|>=|达到)\s*(\d+(?:\.\d+)?)", "学分不低于 {}"),
]


def parse_requirements(text: str) -> List[Dict[str, Any]]:
    """把奖学金要求（自然语言）解析成可判定的条款。"""

    text = str(text or "")
    clauses: List[Dict[str, Any]] = []
    for kind, pattern, label in _CLAUSE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            clauses.append({"kind": kind, "value": float(match.group(1)), "label": label.format(match.group(1)), "raw": match.group(0)})
    if re.search(r"(无挂科|不得有挂科|不能有挂科|无不及格|不得不及格|全部及格)", text):
        clauses.append({"kind": "no_fail", "value": None, "label": "无挂科", "raw": "无挂科"})
    if re.search(r"(无违纪|无处分|未受过处分|无纪律处分)", text):
        clauses.append({"kind": "no_discipline", "value": None, "label": "无违纪处分", "raw": "无违纪"})
    if re.search(r"必修[^。；;\n]{0,10}(?:全部|均)?(?:及格|通过|合格|无挂科)", text):
        clauses.append({"kind": "required_pass", "value": None, "label": "必修课全部及格", "raw": "必修课全部及格"})
    # 去重
    seen = set()
    unique: List[Dict[str, Any]] = []
    for clause in clauses:
        key = (clause["kind"], clause["value"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(clause)
    return unique


def evaluate_requirements(
    clauses: Sequence[Dict[str, Any]],
    stats: Dict[str, Any],
    *,
    ranking: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """逐条判定是否满足。``ok=None`` 表示缺少数据无法判定。"""

    results: List[Dict[str, Any]] = []
    for clause in clauses:
        kind = clause["kind"]
        value = clause.get("value")
        ok: Optional[bool] = None
        actual: Any = None

        if kind == "avg":
            actual = stats.get("average")
            ok = None if actual is None else actual >= float(value)
        elif kind == "gpa":
            actual = stats.get("gpa")
            ok = None if actual is None else actual >= float(value)
        elif kind == "min_score":
            actual = stats.get("min_score")
            ok = None if actual is None else actual >= float(value)
        elif kind == "credit":
            actual = stats.get("total_credit")
            ok = None if actual is None else actual >= float(value)
        elif kind == "no_fail":
            actual = len(stats.get("failed") or [])
            ok = actual == 0
        elif kind == "no_discipline":
            actual = len(stats.get("discipline") or [])
            ok = actual == 0
        elif kind == "required_pass":
            actual = len(stats.get("required_failed") or [])
            ok = actual == 0
        elif kind in {"rank_n", "rank_pct"}:
            rank = (ranking or {}).get("rank")
            total = (ranking or {}).get("total")
            if rank is None:
                ok = None
            elif kind == "rank_n":
                actual = rank
                ok = rank <= float(value)
            else:
                if not total:
                    ok = None
                else:
                    actual = round(rank / total * 100, 2)
                    ok = actual <= float(value)

        results.append({**clause, "ok": ok, "actual": actual})
    return results


# --------------------------------------------------------------------- 差异
def diff_tables(left: Table, right: Table, *, limit: int = 40) -> Dict[str, Any]:
    """逐单元格比较两张表。"""

    left_rows = left.body
    right_rows = right.body
    max_rows = max(len(left_rows), len(right_rows))
    width = max(len(left.header), len(right.header), max((len(r) for r in left_rows + right_rows), default=0))

    changes: List[Dict[str, Any]] = []
    for r in range(max_rows):
        left_row = left_rows[r] if r < len(left_rows) else []
        right_row = right_rows[r] if r < len(right_rows) else []
        for c in range(width):
            a = str(left_row[c]).strip() if c < len(left_row) else ""
            b = str(right_row[c]).strip() if c < len(right_row) else ""
            if a != b:
                header = left.header[c] if c < len(left.header) else (right.header[c] if c < len(right.header) else col_letter(c))
                changes.append(
                    {
                        "cell": f"{col_letter(c)}{left.header_index + r + 2}",
                        "column": header,
                        "row": r + 1,
                        "left": a,
                        "right": b,
                    }
                )
                if len(changes) >= limit:
                    break
        if len(changes) >= limit:
            break

    added_rows = max(0, len(right_rows) - len(left_rows))
    removed_rows = max(0, len(left_rows) - len(right_rows))
    return {
        "mode": "table",
        "left": {"name": left.path.name if left.path else "", "sheet": left.sheet, "rows": len(left_rows)},
        "right": {"name": right.path.name if right.path else "", "sheet": right.sheet, "rows": len(right_rows)},
        "changes": changes,
        "change_count": len(changes),
        "truncated": len(changes) >= limit,
        "added_rows": added_rows,
        "removed_rows": removed_rows,
        "identical": not changes and added_rows == 0 and removed_rows == 0,
    }


def diff_texts(left_text: str, right_text: str, *, limit: int = 200) -> Dict[str, Any]:
    """文本按行比较。"""

    left_lines = left_text.splitlines()
    right_lines = right_text.splitlines()
    diff = list(
        difflib.unified_diff(left_lines, right_lines, fromfile="左", tofile="右", lineterm="", n=2)
    )
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return {
        "mode": "text",
        "lines": diff[:limit],
        "truncated": len(diff) > limit,
        "added": added,
        "removed": removed,
        "identical": left_text == right_text,
    }


def diff_files(left_path: Path, right_path: Path) -> Dict[str, Any]:
    """按类型选择比较方式。"""

    left_kind = office_io.kind_of(left_path)
    right_kind = office_io.kind_of(right_path)
    if left_kind in {"xlsx", "csv"} and right_kind in {"xlsx", "csv"}:
        return diff_tables(load_table(left_path), load_table(right_path))
    left_text = office_io.read_any(left_path, max_chars=200000).get("text") or ""
    right_text = office_io.read_any(right_path, max_chars=200000).get("text") or ""
    result = diff_texts(left_text, right_text)
    result["left"] = {"name": left_path.name, "kind": left_kind, "size": left_path.stat().st_size}
    result["right"] = {"name": right_path.name, "kind": right_kind, "size": right_path.stat().st_size}
    return result


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------- 格式规则
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6}


def parse_format_rules(text: str) -> Dict[str, Any]:
    """把「表格格式要求」解析成规则字典。"""

    text = str(text or "")
    rules: Dict[str, Any] = {
        "header_row": 1,
        "header_bold": True,
        "header_fill": "4472C4",
        "freeze_header": True,
        "autofilter": True,
        "borders": True,
        "align": "left",
        "col_widths": {},
        "number_formats": {},
        "percent_columns": [],
        "keep_columns": [],
        "rename": {},
        "drop_empty": False,
        "drop_duplicates": False,
        "sort_by": None,
        "row_height": None,
        "notes": [],
    }

    if re.search(r"(不冻结|取消冻结)", text):
        rules["freeze_header"] = False
    elif re.search(r"冻结(?:首行|第一行|窗格|表头)", text):
        rules["freeze_header"] = True

    if re.search(r"(不要|取消|关闭)(?:自动)?筛选", text):
        rules["autofilter"] = False
    elif re.search(r"(自动筛选|开启筛选|加筛选)", text):
        rules["autofilter"] = True

    if re.search(r"(不要|取消|去掉|不加)(?:边框|框线)", text):
        rules["borders"] = False
    elif re.search(r"(加边框|添加边框|边框)", text):
        rules["borders"] = True

    if re.search(r"(居中|中间对齐)", text):
        rules["align"] = "center"
    elif re.search(r"(左对齐)", text):
        rules["align"] = "left"
    elif re.search(r"(右对齐)", text):
        rules["align"] = "right"

    if re.search(r"(表头|标题)[^。；;\n]{0,4}(加粗|粗体)", text) or re.search(r"(加粗|粗体)[^。；;\n]{0,4}(表头|标题)", text):
        rules["header_bold"] = True

    match = re.search(r"(?:表头|标题|表头行|标题行)[^。；;\n]{0,6}第\s*(\d+)\s*行", text) or re.search(
        r"第\s*(\d+)\s*行(?:作为|是)?(?:表头|标题)", text
    )
    if match:
        rules["header_row"] = max(1, int(match.group(1)))

    for match in re.finditer(r"([A-Za-z])\s*列[^。；;\n]{0,6}?宽(?:度)?[^0-9]{0,4}(\d+(?:\.\d+)?)", text):
        rules["col_widths"][match.group(1).upper()] = float(match.group(2))
    for match in re.finditer(r"列宽[^0-9]{0,6}(\d+(?:\.\d+)?)", text):
        if not rules["col_widths"]:
            rules["col_widths"]["*"] = float(match.group(1))

    match = re.search(r"保留\s*([一二两三四五六\d])\s*位小数", text)
    if match:
        raw = match.group(1)
        digits = _CN_NUM.get(raw, None) or int(raw)
        fmt = "0." + "0" * int(digits)
        rules["number_formats"]["*"] = fmt
        rules["notes"].append(f"数值保留 {digits} 位小数")

    if re.search(r"百分比", text):
        rules["number_formats"]["*"] = "0.00%"
        rules["percent_columns"].append("*")

    if re.search(r"千分位", text):
        rules["number_formats"]["*"] = "#,##0.00"

    match = re.search(r"(?:按|依据)\s*([^\s，,。；;]{1,12}?)\s*(?:列)?\s*(升序|降序|从大到小|从小到大)", text)
    if match:
        direction = "desc" if match.group(2) in {"降序", "从大到小"} else "asc"
        rules["sort_by"] = {"column": match.group(1), "direction": direction}

    if re.search(r"删除(?:重复行|重复项)|去重", text):
        rules["drop_duplicates"] = True
    if re.search(r"删除空行|去掉空行", text):
        rules["drop_empty"] = True

    match = re.search(r"只保留\s*(?:列)?\s*([A-Za-z](?:\s*[,，、]\s*[A-Za-z])*)", text)
    if match:
        rules["keep_columns"] = [part.strip().upper() for part in re.split(r"[,，、]", match.group(1)) if part.strip()]

    for match in re.finditer(r"(?:把)?列?\s*([A-Za-z])\s*(?:列)?\s*(?:改为|重命名为|改成)\s*([^\s，,。；;]+)", text):
        rules["rename"][match.group(1).upper()] = match.group(2)

    return rules


def apply_format_rules(table: Table, rules: Dict[str, Any]) -> Tuple[List[List[str]], Dict[str, Any]]:
    """按规则重排/清洗数据，返回 (新行, 写出选项)。"""

    header_index = max(0, int(rules.get("header_row", 1)) - 1)
    header = [str(cell).strip() for cell in table.rows[header_index]] if header_index < len(table.rows) else []
    body = [list(row) for row in table.rows[header_index + 1 :]]

    if rules.get("drop_empty"):
        body = [row for row in body if any(str(cell).strip() for cell in row)]

    if rules.get("keep_columns"):
        indexes = [col_index(letter) for letter in rules["keep_columns"]]
        header = [header[i] if i < len(header) else col_letter(i) for i in indexes]
        body = [[row[i] if i < len(row) else "" for i in indexes] for row in body]

    for letter, new_name in (rules.get("rename") or {}).items():
        index = col_index(letter)
        if index < len(header):
            header[index] = new_name

    if rules.get("drop_duplicates"):
        seen = set()
        unique_rows = []
        for row in body:
            key = tuple(str(cell).strip() for cell in row)
            if key in seen:
                continue
            seen.add(key)
            unique_rows.append(row)
        body = unique_rows

    sort_rule = rules.get("sort_by")
    if sort_rule:
        column = str(sort_rule.get("column") or "").strip()
        index = -1
        if len(column) == 1 and column.isalpha():
            index = col_index(column)
        else:
            for i, title in enumerate(header):
                if column and (column in title or title in column):
                    index = i
                    break
        if index >= 0:
            reverse = sort_rule.get("direction") == "desc"

            def sort_key(row: List[str]) -> Tuple[int, Any]:
                value = row[index] if index < len(row) else ""
                number = to_number(value)
                return (0, number) if number is not None else (1, str(value))

            body.sort(key=sort_key, reverse=reverse)

    rows = [header] + body if header else body

    # 数字格式：按列名或通配
    formats: Dict[str, str] = {}
    wildcard_format = (rules.get("number_formats") or {}).get("*")
    for letter, fmt in (rules.get("number_formats") or {}).items():
        if letter != "*":
            formats[letter] = fmt
    if wildcard_format:
        for index, title in enumerate(header):
            if any(hint in title for hint in ("成绩", "分数", "学分", "绩点", "金额", "得分", "平均")):
                formats[col_letter(index)] = wildcard_format
        if not formats:
            formats = {col_letter(i): wildcard_format for i in range(len(header))}

    widths: Dict[Any, float] = dict(rules.get("col_widths") or {})

    options = {
        "header": True,
        "header_bold": bool(rules.get("header_bold", True)),
        "header_fill": str(rules.get("header_fill") or "4472C4").replace("#", "")[:6] or "4472C4",
        "freeze_header": bool(rules.get("freeze_header", True)),
        "autofilter": bool(rules.get("autofilter", True)),
        "borders": bool(rules.get("borders", True)),
        "align": str(rules.get("align") or "left"),
        "number_formats": formats,
        "col_widths": widths,
    }
    return rows, options


# ------------------------------------------------------------------- 排名
def ranking_for_student(rank_table: "Table", transcript: Optional["Table"] = None) -> Dict[str, Any]:
    """从排名表里找出成绩单主人的名次。

    排名表通常是「学号/姓名/排名」三列。优先按姓名匹配成绩单第一行的姓名，
    找不到就用学号匹配，再找不到退回第一行；``total`` 取排名表的数据行数。
    """

    roles = rank_table.roles()
    rank_col = roles.get("rank", -1)
    if rank_col < 0:
        return {"rank": None, "total": len(rank_table.body), "name": ""}

    def _cell(row: Sequence[Any], index: int) -> str:
        return str(row[index]).strip() if 0 <= index < len(row) else ""

    name_col = roles.get("name", -1)
    sid_col = roles.get("sid", -1)
    target_name = ""
    target_sid = ""
    if transcript is not None:
        t_roles = transcript.roles()
        first = transcript.body[0] if transcript.body else []
        target_name = _cell(first, t_roles.get("name", -1))
        target_sid = _cell(first, t_roles.get("sid", -1))

    chosen: Optional[Sequence[Any]] = None
    for row in rank_table.body:
        if target_sid and sid_col >= 0 and _cell(row, sid_col) == target_sid:
            chosen = row
            break
        if target_name and name_col >= 0 and _cell(row, name_col) == target_name:
            chosen = row
            break
    if chosen is None and rank_table.body:
        chosen = rank_table.body[0]

    value = to_number(_cell(chosen, rank_col)) if chosen is not None else None
    return {
        "rank": int(value) if value is not None else None,
        "total": len(rank_table.body),
        "name": _cell(chosen, name_col) if chosen is not None and name_col >= 0 else target_name,
    }
