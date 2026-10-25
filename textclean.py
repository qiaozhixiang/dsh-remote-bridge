"""把机器友好的回复洗成人能直接读的纯文本。

插件把回复发到 QQ 这类纯文本通道时，常会带出一堆对阅读无用的「结构代码」：
GenUI 卡片、JSON/YAML 片段、XML 标签、Markdown 标题与加粗、表格分隔线等。
这里统一做清洗，只保留信息本身。用户明确要代码时可以用 ``allow_code=True``
关掉代码块相关的裁剪。
"""

from __future__ import annotations

import json
import re
from typing import Iterable, List

__all__ = ["clean_reply", "wants_code", "strip_code_fences", "collapse_blank"]

# ---------------------------------------------------------------- 基础正则

_THINK_BLOCK = re.compile(
    r"<(think|thinking|reasoning|analysis|thought)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_THINK_TAG = re.compile(r"</?(?:think|thinking|reasoning|analysis|thought)\b[^>]*>", re.IGNORECASE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"</?[a-zA-Z][\w:.-]*(?:\s[^<>]*)?/?>")
_FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.MULTILINE)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD2 = re.compile(r"__(.+?)__", re.DOTALL)
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+", re.MULTILINE)
_TABLE_SEP = re.compile(r"^[ \t]*\|?[ \t:|-]*-{2,}[ \t:|-]*\|?[ \t]*\n?", re.MULTILINE)
_RULE = re.compile(r"^[ \t]*([-*_])\1{2,}[ \t]*\n?", re.MULTILINE)
_ZW = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK = re.compile(r"\n{3,}")

# 语言标记属于「结构数据」的围栏内容，除非用户要代码，否则整块丢弃
_STRUCTURE_LANGS = {
    "json", "json5", "jsonc", "ndjson", "csvdata",
}
# 出现在围栏里的「界面/结构」标记
_STRUCTURE_KEYS = ('"type"', '"items"', '"columns"', '"rows"', '"steps"', '"content"')

_CODE_HINTS = (
    "代码", "脚本", "命令", "源码", "函数", "正则", "sql", "json", "配置",
    "示例", "模板", "代码块", "怎么写", "怎么改", "如何写", "如何改",
    "格式示例", "prompt", "公式", "伪代码", "字段", "schema", "接口",
)


def wants_code(text: str) -> bool:
    """用户这句话是否在要代码/结构化内容（要的话就别乱删）。"""

    probe = (text or "").lower()
    return any(hint in probe for hint in _CODE_HINTS)


def _looks_structural(text: str) -> bool:
    """判断一段围栏内容是不是机器结构（JSON/卡片定义），而不是散文。"""

    body = (text or "").strip()
    if not body:
        return True
    if body[:1] in ("{", "["):
        try:
            json.loads(body)
            return True
        except Exception:  # noqa: BLE001
            pass
    hits = sum(1 for key in _STRUCTURE_KEYS if key in body)
    return hits >= 2


def _render_ui(node: Any, out: List[str]) -> None:
    """把 GenUI 卡片的 JSON 还原成几行纯文本（保留信息，丢掉结构）。"""

    if isinstance(node, list):
        for child in node:
            _render_ui(child, out)
        return
    if not isinstance(node, dict):
        if node not in (None, ""):
            out.append(str(node))
        return
    kind = str(node.get("type") or "")
    if kind == "table":
        columns = node.get("columns") or []
        if columns:
            out.append(" | ".join(str(col) for col in columns))
        for row in node.get("rows") or []:
            if isinstance(row, (list, tuple)):
                out.append(" | ".join(str(cell) for cell in row))
        return
    if kind == "steps":
        for index, step in enumerate(node.get("steps") or [], 1):
            if isinstance(step, dict):
                head = str(step.get("title") or "").strip()
                desc = str(step.get("desc") or step.get("content") or "").strip()
                out.append(f"{index}. {head}" + (f"：{desc}" if desc else ""))
            else:
                out.append(f"{index}. {step}")
        return
    title = str(node.get("title") or node.get("label") or "").strip()
    body = str(node.get("content") or node.get("desc") or node.get("text") or "").strip()
    if title and body:
        out.append(f"{title}：{body}")
    elif title:
        out.append(title)
    elif body:
        out.append(body)
    for key in ("items", "children", "sections", "fields", "cards"):
        for child in node.get(key) or []:
            _render_ui(child, out)


def _strip_fences(text: str, allow_code: bool) -> str:
    """处理 ```lang … ``` 围栏：结构数据整块丢弃，普通代码保留正文。"""

    def repl(match: "re.Match[str]") -> str:
        lang = (match.group(1) or "").strip().lower().split()[0] if match.group(1).strip() else ""
        body = match.group(2) or ""
        if lang.startswith("dsh-") or lang in {"genui", "ui"}:
            try:
                decoded = json.loads(body.strip())
            except Exception:  # noqa: BLE001
                return ""
            lines: List[str] = []
            _render_ui(decoded, lines)
            return "\n".join(line for line in lines if line.strip())
        if lang in _STRUCTURE_LANGS or lang.startswith("dsh-"):
            return ""
        if _looks_structural(body) and not allow_code:
            return ""
        return body if allow_code or not _looks_structural(body) else ""

    return _FENCE.sub(repl, text)


def _strip_json_lines(text: str, allow_code: bool) -> str:
    """删掉没有围栏包裹、但明显是 JSON 对象的连续行。"""

    if allow_code:
        return text
    out: List[str] = []
    buffer: List[str] = []
    depth = 0
    for line in text.splitlines():
        stripped = line.strip()
        if depth or stripped[:1] in ("{", "["):
            buffer.append(line)
            depth += line.count("{") + line.count("[") - line.count("}") - line.count("]")
            if depth <= 0:
                blob = "\n".join(buffer).strip()
                buffer = []
                depth = 0
                try:
                    json.loads(blob)
                    continue  # 是纯 JSON，整块丢掉
                except Exception:  # noqa: BLE001
                    out.extend(blob.splitlines())
                continue
            continue
        if buffer:
            out.extend(buffer)
            buffer = []
        out.append(line)
    if buffer:
        out.extend(buffer)
    return "\n".join(out)


def collapse_blank(text: str) -> str:
    """把连续空行压成一行，去掉行尾空格。"""

    cleaned = _TRAILING_WS.sub("", text or "")
    cleaned = _BLANK.sub("\n\n", cleaned)
    return cleaned.strip()


def strip_code_fences(text: str, *, allow_code: bool = False) -> str:
    """只处理围栏，不碰别的排版。"""

    return _strip_fences(text or "", allow_code)


def clean_reply(
    text: str,
    *,
    allow_code: bool = False,
    max_chars: int = 0,
    keep_empty_note: str = "",
    strip_markdown: bool = True,
) -> str:
    """把一段回复洗成纯文本。

    ``allow_code`` 为真时保留代码围栏内容（用户明确要代码的场景）。
    ``max_chars`` 大于 0 时按字符数截断。
    ``strip_markdown`` 为假时只清结构（卡片/JSON/标签），保留原有的排版符号。
    """

    raw = str(text or "")
    if not raw.strip():
        return ""
    cleaned = _ZW.sub("", raw)
    cleaned = _COMMENT.sub("", cleaned)
    cleaned = _SCRIPT.sub("", cleaned)
    cleaned = _THINK_BLOCK.sub("", cleaned)
    cleaned = _THINK_TAG.sub("", cleaned)
    cleaned = _strip_fences(cleaned, allow_code)
    cleaned = _strip_json_lines(cleaned, allow_code)
    # 标签只脱壳、保留里面的文字（<result>正文</result> -> 正文）
    cleaned = _TAG.sub("", cleaned)
    if strip_markdown:
        cleaned = _IMAGE.sub("", cleaned)
        cleaned = _LINK.sub(lambda m: f"{m.group(1)}（{m.group(2)}）", cleaned)
        cleaned = _HEADING.sub("", cleaned)
        cleaned = _BOLD.sub(r"\1", cleaned)
        cleaned = _BOLD2.sub(r"\1", cleaned)
        cleaned = _ITALIC.sub(r"\1", cleaned)
        cleaned = _BULLET.sub("· ", cleaned)
        cleaned = _RULE.sub("", cleaned)
        cleaned = _TABLE_SEP.sub("", cleaned)
        if not allow_code:
            cleaned = _INLINE_CODE.sub(r"\1", cleaned)
    cleaned = collapse_blank(cleaned)
    if max_chars and len(cleaned) > max_chars:
        cleaned = cleaned[: int(max_chars)].rstrip() + "…"
    if not cleaned:
        return keep_empty_note or ""
    return cleaned
