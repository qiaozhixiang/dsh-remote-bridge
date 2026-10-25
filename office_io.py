"""办公文件读写引擎（纯标准库实现）。

支持读取：docx / xlsx / pptx / pdf（尽力而为）/ csv / txt / md / json / zip
支持生成：docx / xlsx / pptx / zip / csv / md / txt

刻意不依赖 python-docx / openpyxl / python-pptx / pypdf：
MaiBot 的 venv 里默认没有这些包，而插件要求「放进 plugins 就能用」。
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

DOCX_SUFFIXES = {".docx", ".docm"}
XLSX_SUFFIXES = {".xlsx", ".xlsm"}
PPTX_SUFFIXES = {".pptx", ".pptm"}
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".log", ".ini", ".cfg", ".yaml", ".yml", ".toml", ".py", ".js", ".ts", ".css", ".html", ".xml"}
CSV_SUFFIXES = {".csv", ".tsv"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
ARCHIVE_SUFFIXES = {".zip"}

KIND_LABELS = {
    "docx": "Word 文档",
    "xlsx": "Excel 表格",
    "pptx": "PowerPoint 演示文稿",
    "pdf": "PDF 文档",
    "csv": "CSV 表格",
    "text": "文本",
    "json": "JSON",
    "zip": "ZIP 压缩包",
    "image": "图片",
    "other": "文件",
}


class OfficeError(Exception):
    """办公文件操作失败。"""


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def kind_of(path: Path | str) -> str:
    """按后缀判断文件类型。"""

    suffix = Path(str(path)).suffix.lower()
    if suffix in DOCX_SUFFIXES:
        return "docx"
    if suffix in XLSX_SUFFIXES:
        return "xlsx"
    if suffix in PPTX_SUFFIXES:
        return "pptx"
    if suffix == ".pdf":
        return "pdf"
    if suffix in CSV_SUFFIXES:
        return "csv"
    if suffix == ".json":
        return "json"
    if suffix in ARCHIVE_SUFFIXES:
        return "zip"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in TEXT_SUFFIXES:
        return "text"
    return "other"


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, "文件")


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _esc(text: Any) -> str:
    """XML 文本转义。"""

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _read_xml(archive: zipfile.ZipFile, name: str) -> Optional[ET.Element]:
    try:
        raw = archive.read(name)
    except KeyError:
        return None
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        return None


def _text_of(element: Optional[ET.Element], tag: str) -> str:
    if element is None:
        return ""
    return "".join(node.text or "" for node in element.iter(f"{{{tag}}}t"))


def _rel_targets(archive: zipfile.ZipFile, part: str) -> Dict[str, str]:
    """读取某个 part 的 .rels，返回 id → target（已相对化）。"""

    base = Path(part)
    rels_name = str(base.parent / "_rels" / f"{base.name}.rels").replace("\\", "/")
    root = _read_xml(archive, rels_name)
    result: Dict[str, str] = {}
    if root is None:
        return result
    for rel in root.findall(f"{{{PKG_REL_NS}}}Relationship"):
        rid = rel.get("Id") or ""
        target = rel.get("Target") or ""
        if not rid or not target:
            continue
        if target.startswith("/"):
            resolved = target.lstrip("/")
        else:
            resolved = str((base.parent / target)).replace("\\", "/")
        # 规范化 ../ 片段
        parts: List[str] = []
        for piece in resolved.split("/"):
            if piece in ("", "."):
                continue
            if piece == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(piece)
        result[rid] = "/".join(parts)
    return result


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def read_docx(path: Path) -> Dict[str, Any]:
    """读取 docx：段落（含样式）、表格、纯文本。"""

    paragraphs: List[Dict[str, str]] = []
    tables: List[List[List[str]]] = []
    try:
        with zipfile.ZipFile(path) as archive:
            root = _read_xml(archive, "word/document.xml")
    except zipfile.BadZipFile as exc:
        raise OfficeError(f"不是有效的 docx 文件：{exc}") from exc
    if root is None:
        raise OfficeError("docx 缺少 word/document.xml")

    body = root.find(f"{{{W_NS}}}body")
    if body is None:
        body = root

    def walk(node: ET.Element) -> None:
        for child in list(node):
            tag = child.tag
            if tag == f"{{{W_NS}}}p":
                texts = [t.text or "" for t in child.iter(f"{{{W_NS}}}t")]
                style = ""
                ppr = child.find(f"{{{W_NS}}}pPr")
                if ppr is not None:
                    pstyle = ppr.find(f"{{{W_NS}}}pStyle")
                    if pstyle is not None:
                        style = pstyle.get(f"{{{W_NS}}}val") or ""
                line = "".join(texts)
                if line.strip() or style.lower().startswith("heading"):
                    paragraphs.append({"text": line, "style": style})
            elif tag == f"{{{W_NS}}}tbl":
                rows: List[List[str]] = []
                for tr in child.findall(f"{{{W_NS}}}tr"):
                    cells: List[str] = []
                    for tc in tr.findall(f"{{{W_NS}}}tc"):
                        cells.append("".join(t.text or "" for t in tc.iter(f"{{{W_NS}}}t")))
                    if cells:
                        rows.append(cells)
                if rows:
                    tables.append(rows)
            else:
                walk(child)

    walk(body)

    lines: List[str] = []
    for item in paragraphs:
        style = item["style"].lower()
        text = item["text"]
        if style.startswith("heading"):
            level = "".join(ch for ch in style if ch.isdigit()) or "1"
            lines.append("#" * min(6, int(level)) + " " + text)
        else:
            lines.append(text)
    for index, table in enumerate(tables, start=1):
        lines.append(f"[表格 {index}]")
        for row in table:
            lines.append(" | ".join(row))

    return {
        "kind": "docx",
        "paragraphs": paragraphs,
        "tables": tables,
        "text": "\n".join(lines).strip(),
        "meta": {"paragraphs": len(paragraphs), "tables": len(tables)},
    }


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> List[str]:
    root = _read_xml(archive, "xl/sharedStrings.xml")
    if root is None:
        return []
    values: List[str] = []
    for si in root.findall(f"{{{S_NS}}}si"):
        values.append("".join(t.text or "" for t in si.iter(f"{{{S_NS}}}t")))
    return values


def _cell_column(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch.upper()) - 64)
    return max(0, index - 1)


def _read_sheet(archive: zipfile.ZipFile, part: str, shared: List[str]) -> List[List[str]]:
    root = _read_xml(archive, part)
    if root is None:
        return []
    rows: List[List[str]] = []
    sheet_data = root.find(f"{{{S_NS}}}sheetData")
    if sheet_data is None:
        return rows
    for row in sheet_data.findall(f"{{{S_NS}}}row"):
        cells: Dict[int, str] = {}
        for cell in row.findall(f"{{{S_NS}}}c"):
            ref = cell.get("r") or ""
            col = _cell_column(ref) if ref else len(cells)
            ctype = cell.get("t") or ""
            value = ""
            if ctype == "inlineStr":
                value = "".join(t.text or "" for t in cell.iter(f"{{{S_NS}}}t"))
            else:
                vnode = cell.find(f"{{{S_NS}}}v")
                raw = (vnode.text or "") if vnode is not None else ""
                if ctype == "s":
                    try:
                        value = shared[int(raw)]
                    except (ValueError, IndexError):
                        value = ""
                elif ctype == "b":
                    value = "TRUE" if raw == "1" else "FALSE"
                else:
                    value = raw
            cells[col] = value
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])
    return rows


def read_xlsx(path: Path) -> Dict[str, Any]:
    """读取 xlsx：每个工作表的二维表。"""

    try:
        with zipfile.ZipFile(path) as archive:
            shared = _xlsx_shared_strings(archive)
            workbook = _read_xml(archive, "xl/workbook.xml")
            rels = _rel_targets(archive, "xl/workbook.xml")
            sheets: List[Dict[str, Any]] = []
            if workbook is not None:
                for index, sheet in enumerate(workbook.findall(f"{{{S_NS}}}sheets/{{{S_NS}}}sheet"), start=1):
                    name = sheet.get("name") or f"Sheet{index}"
                    rid = sheet.get(f"{{{R_NS}}}id") or ""
                    part = rels.get(rid) or f"xl/worksheets/sheet{index}.xml"
                    rows = _read_sheet(archive, part, shared)
                    sheets.append({"name": name, "rows": rows})
            if not sheets:
                for index in range(1, 30):
                    part = f"xl/worksheets/sheet{index}.xml"
                    try:
                        archive.getinfo(part)
                    except KeyError:
                        break
                    sheets.append({"name": f"Sheet{index}", "rows": _read_sheet(archive, part, shared)})
    except zipfile.BadZipFile as exc:
        raise OfficeError(f"不是有效的 xlsx 文件：{exc}") from exc

    lines: List[str] = []
    for sheet in sheets:
        lines.append(f"[工作表] {sheet['name']}")
        for row in sheet["rows"]:
            lines.append(" | ".join(row))
    return {
        "kind": "xlsx",
        "sheets": sheets,
        "text": "\n".join(lines).strip(),
        "meta": {"sheets": len(sheets), "rows": sum(len(s["rows"]) for s in sheets)},
    }


def read_pptx(path: Path) -> Dict[str, Any]:
    """读取 pptx：每页的文字。"""

    try:
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)]
            names.sort(key=lambda n: int(re.findall(r"(\d+)", n)[-1]))
            slides: List[Dict[str, Any]] = []
            for index, name in enumerate(names, start=1):
                root = _read_xml(archive, name)
                if root is None:
                    continue
                texts = [t.text or "" for t in root.iter(f"{{{A_NS}}}t")]
                slides.append({"index": index, "texts": [t for t in texts if t.strip()]})
    except zipfile.BadZipFile as exc:
        raise OfficeError(f"不是有效的 pptx 文件：{exc}") from exc

    lines: List[str] = []
    for slide in slides:
        lines.append(f"[第 {slide['index']} 页]")
        lines.extend(slide["texts"])
    return {
        "kind": "pptx",
        "slides": slides,
        "text": "\n".join(lines).strip(),
        "meta": {"slides": len(slides)},
    }


_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_PDF_TJ = re.compile(rb"\((?:\\.|[^\\()])*\)\s*Tj")
_PDF_TJ_ARRAY = re.compile(rb"\[(.*?)\]\s*TJ", re.S)
_PDF_STRING = re.compile(rb"\((?:\\.|[^\\()])*\)")


def _pdf_unescape(raw: bytes) -> str:
    text = raw.decode("latin-1", "ignore")
    text = text.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
    text = text.replace("\\n", "\n").replace("\\r", "").replace("\\t", "\t")
    return text


def read_pdf(path: Path) -> Dict[str, Any]:
    """尽力从 PDF 抽取文本（仅处理 FlateDecode / 未压缩的内容流）。"""

    data = path.read_bytes()
    chunks: List[str] = []
    for match in _PDF_STREAM.finditer(data):
        body = match.group(1)
        try:
            body = zlib.decompress(body)
        except zlib.error:
            pass
        for token in _PDF_TJ.findall(body):
            chunks.append(_pdf_unescape(token[1:-2]))
        for array in _PDF_TJ_ARRAY.findall(body):
            parts = [_pdf_unescape(p[1:-1]) for p in _PDF_STRING.findall(array)]
            if parts:
                chunks.append("".join(parts))
    text = "\n".join(part for part in chunks if part.strip()).strip()
    return {
        "kind": "pdf",
        "text": text,
        "meta": {"chars": len(text), "note": "PDF 文本为尽力抽取，扫描件可能为空"},
    }


def read_text(path: Path) -> Dict[str, Any]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 不会失败
        text = raw.decode("utf-8", "replace")
    return {"kind": "text", "text": text, "meta": {"chars": len(text), "encoding": encoding}}


def read_csv_file(path: Path) -> Dict[str, Any]:
    result = read_text(path)
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    rows = list(csv.reader(io.StringIO(result["text"]), delimiter=delimiter))
    lines = [" | ".join(row) for row in rows]
    return {
        "kind": "csv",
        "rows": rows,
        "text": "\n".join(lines).strip(),
        "meta": {"rows": len(rows), "columns": max((len(r) for r in rows), default=0)},
    }


def read_json_file(path: Path) -> Dict[str, Any]:
    result = read_text(path)
    try:
        parsed = json.loads(result["text"])
    except json.JSONDecodeError as exc:
        raise OfficeError(f"JSON 解析失败：{exc}") from exc
    pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
    return {"kind": "json", "data": parsed, "text": pretty, "meta": {"chars": len(pretty)}}


def read_zip(path: Path) -> Dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = []
            for info in archive.infolist():
                entries.append(
                    {
                        "name": info.filename,
                        "size": info.file_size,
                        "compressed": info.compress_size,
                        "is_dir": info.is_dir(),
                    }
                )
    except zipfile.BadZipFile as exc:
        raise OfficeError(f"不是有效的 ZIP：{exc}") from exc
    lines = [
        f"{item['name']}{'/' if item['is_dir'] else ''}\t{human_size(item['size'])}"
        for item in entries[:200]
    ]
    if len(entries) > 200:
        lines.append(f"…… 共 {len(entries)} 项")
    return {
        "kind": "zip",
        "entries": entries,
        "text": "\n".join(lines),
        "meta": {"entries": len(entries), "total": sum(e["size"] for e in entries)},
    }


def read_any(path: Path, *, max_chars: int = 400000) -> Dict[str, Any]:
    """按类型读取任意文件。"""

    if not path.exists():
        raise OfficeError(f"文件不存在：{path}")
    if path.is_dir():
        raise OfficeError(f"这是一个目录，不是文件：{path}")
    kind = kind_of(path)
    if kind == "docx":
        result = read_docx(path)
    elif kind == "xlsx":
        result = read_xlsx(path)
    elif kind == "pptx":
        result = read_pptx(path)
    elif kind == "pdf":
        result = read_pdf(path)
    elif kind == "csv":
        result = read_csv_file(path)
    elif kind == "json":
        result = read_json_file(path)
    elif kind == "zip":
        result = read_zip(path)
    elif kind == "image":
        result = {"kind": "image", "text": "", "meta": {"note": "图片文件，无文本内容"}}
    else:
        result = read_text(path)
    if len(result.get("text") or "") > max_chars:
        result["text"] = result["text"][:max_chars] + "\n……（内容过长，已截断）"
        result["meta"] = {**result.get("meta", {}), "truncated": True}
    result["path"] = str(path)
    result["name"] = path.name
    result["size"] = path.stat().st_size
    result["kind_label"] = kind_label(result["kind"])
    return result


def describe(path: Path) -> Dict[str, Any]:
    """文件元信息（不解析内容）。"""

    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size": stat.st_size,
        "size_text": human_size(stat.st_size),
        "mtime": stat.st_mtime,
        "kind": kind_of(path),
        "kind_label": kind_label(kind_of(path)),
    }


# ---------------------------------------------------------------------------
# 生成：docx
# ---------------------------------------------------------------------------

_DOCX_CONTENT_TYPES = (
    XML_HEAD
    + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)

_DOCX_ROOT_RELS = (
    XML_HEAD
    + f'<Relationships xmlns="{PKG_REL_NS}">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)

_DOCX_DOC_RELS = (
    XML_HEAD
    + f'<Relationships xmlns="{PKG_REL_NS}">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    "</Relationships>"
)

_DOCX_STYLES = (
    XML_HEAD
    + f'<w:styles xmlns:w="{W_NS}">'
    "<w:docDefaults><w:rPrDefault><w:rPr>"
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Microsoft YaHei"/>'
    '<w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
    "</w:docDefaults>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
    '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="320" w:after="160"/>'
    '<w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>'
    '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/>'
    '<w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="30"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/>'
    '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="200" w:after="100"/>'
    '<w:outlineLvl w:val="2"/></w:pPr><w:rPr><w:b/><w:sz w:val="26"/></w:rPr></w:style>'
    "</w:styles>"
)


def _docx_paragraph(text: str, *, style: str = "", bullet: bool = False) -> str:
    ppr = ""
    body_text = text
    if style:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
    elif bullet:
        ppr = '<w:pPr><w:ind w:left="420" w:hanging="300"/></w:pPr>'
        body_text = "• " + text
    run = f'<w:r><w:t xml:space="preserve">{_esc(body_text)}</w:t></w:r>'
    return f"<w:p>{ppr}{run}</w:p>"


def _docx_table(rows: Sequence[Sequence[Any]]) -> str:
    border = "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    parts = [
        '<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/>'
        f"<w:tblBorders>{border}</w:tblBorders></w:tblPr>"
    ]
    for row in rows:
        parts.append("<w:tr>")
        for cell in row:
            parts.append(
                '<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>'
                f"<w:p><w:r><w:t xml:space=\"preserve\">{_esc(cell)}</w:t></w:r></w:p></w:tc>"
            )
        parts.append("</w:tr>")
    parts.append("</w:tbl>")
    return "".join(parts)


def create_docx(path: Path, blocks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """按块列表生成 docx。

    block 支持：``heading``（带 level）、``paragraph``、``bullet``、``table``（带 rows）。
    """

    body: List[str] = []
    for block in blocks:
        btype = str(block.get("type") or "paragraph")
        if btype == "heading":
            level = int(block.get("level") or 1)
            style = f"Heading{min(3, max(1, level))}"
            body.append(_docx_paragraph(str(block.get("text") or ""), style=style))
        elif btype == "bullet":
            body.append(_docx_paragraph(str(block.get("text") or ""), bullet=True))
        elif btype == "table":
            rows = block.get("rows") or []
            if rows:
                body.append(_docx_table(rows))
                body.append(_docx_paragraph(""))
        else:
            body.append(_docx_paragraph(str(block.get("text") or "")))

    sect = (
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
        'w:header="851" w:footer="992" w:gutter="0"/></w:sectPr>'
    )
    document = (
        XML_HEAD
        + f'<w:document xmlns:w="{W_NS}"><w:body>'
        + "".join(body)
        + sect
        + "</w:body></w:document>"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        archive.writestr("_rels/.rels", _DOCX_ROOT_RELS)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", _DOCX_DOC_RELS)
        archive.writestr("word/styles.xml", _DOCX_STYLES)
    return {"kind": "docx", "path": str(path), "blocks": len(blocks), "size": path.stat().st_size}


def docx_from_markdown(path: Path, markdown: str, title: str = "") -> Dict[str, Any]:
    """把轻量 Markdown 转成 docx。

    支持 ``#``/``##``/``###`` 标题、``- `` 列表、``| a | b |`` 表格、其余为正文。
    """

    blocks: List[Dict[str, Any]] = []
    if title:
        blocks.append({"type": "heading", "level": 1, "text": title})
    table_rows: List[List[str]] = []

    def flush_table() -> None:
        nonlocal table_rows
        if table_rows:
            blocks.append({"type": "table", "rows": table_rows})
            table_rows = []

    for raw in markdown.replace("\\n", "\n").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_table()
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= {"-", ":", " "} for c in cells):
                continue
            table_rows.append(cells)
            continue
        flush_table()
        if stripped.startswith("### "):
            blocks.append({"type": "heading", "level": 3, "text": stripped[4:].strip()})
        elif stripped.startswith("## "):
            blocks.append({"type": "heading", "level": 2, "text": stripped[3:].strip()})
        elif stripped.startswith("# "):
            blocks.append({"type": "heading", "level": 1, "text": stripped[2:].strip()})
        elif stripped.startswith(("- ", "* ", "· ")):
            blocks.append({"type": "bullet", "text": stripped[2:].strip()})
        else:
            blocks.append({"type": "paragraph", "text": stripped})
    flush_table()
    return create_docx(path, blocks)


# ---------------------------------------------------------------------------
# 生成：xlsx
# ---------------------------------------------------------------------------

_XLSX_CONTENT_TYPES = (
    XML_HEAD
    + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '{sheets}'
    "</Types>"
)

_XLSX_ROOT_RELS = (
    XML_HEAD
    + f'<Relationships xmlns="{PKG_REL_NS}">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)


def _col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _xlsx_cell(ref: str, value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = str(value)
    if text.startswith("="):
        return f'<c r="{ref}"><f>{_esc(text[1:])}</f></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_esc(text)}</t></is></c>'


def _xlsx_sheet_xml(rows: Sequence[Sequence[Any]]) -> str:
    widths: Dict[int, int] = {}
    for row in rows:
        for index, value in enumerate(row):
            width = len(str(value)) if value is not None else 0
            widths[index] = max(widths.get(index, 8), min(60, width + 2))
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{width}" customWidth="1"/>'
        for i, width in sorted(widths.items())
    )
    body: List[str] = []
    for r_index, row in enumerate(rows, start=1):
        cells = "".join(
            _xlsx_cell(f"{_col_letter(c_index)}{r_index}", value)
            for c_index, value in enumerate(row)
        )
        body.append(f'<row r="{r_index}">{cells}</row>')
    return (
        XML_HEAD
        + f'<worksheet xmlns="{S_NS}" xmlns:r="{R_NS}">'
        + (f"<cols>{cols}</cols>" if cols else "")
        + "<sheetData>"
        + "".join(body)
        + "</sheetData></worksheet>"
    )


def create_xlsx(path: Path, sheets: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """生成 xlsx。``sheets`` 形如 ``[{"name": "Sheet1", "rows": [[...]]}]``。"""

    sheets = list(sheets) or [{"name": "Sheet1", "rows": []}]
    overrides: List[str] = []
    rels: List[str] = []
    workbook_sheets: List[str] = []
    payload: Dict[str, str] = {}

    for index, sheet in enumerate(sheets, start=1):
        name = str(sheet.get("name") or f"Sheet{index}")[:31]
        rows = sheet.get("rows") or []
        part = f"xl/worksheets/sheet{index}.xml"
        payload[part] = _xlsx_sheet_xml(rows)
        overrides.append(
            f'<Override PartName="/{part}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        rels.append(
            f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        )
        workbook_sheets.append(f'<sheet name="{_esc(name)}" sheetId="{index}" r:id="rId{index}"/>')

    workbook = (
        XML_HEAD
        + f'<workbook xmlns="{S_NS}" xmlns:r="{R_NS}"><sheets>'
        + "".join(workbook_sheets)
        + "</sheets></workbook>"
    )
    workbook_rels = (
        XML_HEAD
        + f'<Relationships xmlns="{PKG_REL_NS}">'
        + "".join(rels)
        + "</Relationships>"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _XLSX_CONTENT_TYPES.format(sheets="".join(overrides)))
        archive.writestr("_rels/.rels", _XLSX_ROOT_RELS)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        for part, content in payload.items():
            archive.writestr(part, content)
    return {
        "kind": "xlsx",
        "path": str(path),
        "sheets": len(sheets),
        "size": path.stat().st_size,
    }


def xlsx_from_csv(path: Path, text: str, sheet_name: str = "Sheet1") -> Dict[str, Any]:
    rows = [row for row in csv.reader(io.StringIO(text.replace("\\n", "\n"))) if row]
    return create_xlsx(path, [{"name": sheet_name, "rows": rows}])


# ---------------------------------------------------------------------------
# 生成：pptx
# ---------------------------------------------------------------------------

_PPTX_THEME = (
    XML_HEAD
    + f'<a:theme xmlns:a="{A_NS}" name="Office Theme"><a:themeElements>'
    '<a:clrScheme name="Office">'
    '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
    '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
    '<a:dk2><a:srgbClr val="44546A"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>'
    '<a:accent1><a:srgbClr val="4472C4"/></a:accent1><a:accent2><a:srgbClr val="ED7D31"/></a:accent2>'
    '<a:accent3><a:srgbClr val="A5A5A5"/></a:accent3><a:accent4><a:srgbClr val="FFC000"/></a:accent4>'
    '<a:accent5><a:srgbClr val="5B9BD5"/></a:accent5><a:accent6><a:srgbClr val="70AD47"/></a:accent6>'
    '<a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink>'
    "</a:clrScheme>"
    '<a:fontScheme name="Office">'
    '<a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
    '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont>'
    "</a:fontScheme>"
    '<a:fmtScheme name="Office">'
    "<a:fillStyleLst>"
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"><a:tint val="50000"/><a:satMod val="300000"/></a:schemeClr></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"><a:shade val="50000"/><a:satMod val="200000"/></a:schemeClr></a:solidFill>'
    "</a:fillStyleLst>"
    "<a:lnStyleLst>"
    '<a:ln w="6350" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln>'
    '<a:ln w="12700" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln>'
    '<a:ln w="19050" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln>'
    "</a:lnStyleLst>"
    "<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle>"
    "<a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>"
    "<a:bgFillStyleLst>"
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"><a:tint val="95000"/><a:satMod val="170000"/></a:schemeClr></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"><a:shade val="93000"/><a:satMod val="150000"/></a:schemeClr></a:solidFill>'
    "</a:bgFillStyleLst>"
    "</a:fmtScheme></a:themeElements><a:objectDefaults/><a:extraClrSchemeLst/></a:theme>"
)

_PPTX_GROUP = (
    "<p:nvGrpSpPr><p:cNvPr id=\"1\" name=\"\"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>"
    "<p:grpSpPr><a:xfrm><a:off x=\"0\" y=\"0\"/><a:ext cx=\"0\" cy=\"0\"/>"
    "<a:chOff x=\"0\" y=\"0\"/><a:chExt cx=\"0\" cy=\"0\"/></a:xfrm></p:grpSpPr>"
)

_PPTX_MASTER = (
    XML_HEAD
    + f'<p:sldMaster xmlns:a="{A_NS}" xmlns:r="{R_NS}" xmlns:p="{P_NS}">'
    "<p:cSld>"
    '<p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>'
    "<p:spTree>"
    + _PPTX_GROUP
    + '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title Placeholder 1"/>'
    '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
    '<p:spPr><a:xfrm><a:off x="838200" y="365125"/><a:ext cx="10515600" cy="1325563"/></a:xfrm></p:spPr>'
    '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="zh-CN"/></a:p></p:txBody></p:sp>'
    '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Text Placeholder 2"/>'
    '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
    '<p:spPr><a:xfrm><a:off x="838200" y="1825625"/><a:ext cx="10515600" cy="4351338"/></a:xfrm></p:spPr>'
    '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="zh-CN"/></a:p></p:txBody></p:sp>'
    "</p:spTree></p:cSld>"
    '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
    'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
    '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
    "</p:sldMaster>"
)

_PPTX_LAYOUT = (
    XML_HEAD
    + f'<p:sldLayout xmlns:a="{A_NS}" xmlns:r="{R_NS}" xmlns:p="{P_NS}" type="titleAndBody" preserve="1">'
    '<p:cSld name="Title and Body"><p:spTree>'
    + _PPTX_GROUP
    + '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title 1"/>'
    '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
    '<p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="zh-CN"/></a:p></p:txBody></p:sp>'
    '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Text Placeholder 2"/>'
    '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
    '<p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="zh-CN"/></a:p></p:txBody></p:sp>'
    "</p:spTree></p:cSld>"
    "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
)


def _pptx_text_paragraph(text: str, *, size: int = 2000, bold: bool = False, bullet: bool = False) -> str:
    ppr = (
        '<a:pPr marL="285750" indent="-285750"><a:buFont typeface="Arial"/><a:buChar char="•"/></a:pPr>'
        if bullet
        else "<a:pPr/>"
    )
    bold_attr = ' b="1"' if bold else ""
    rpr = f'<a:rPr lang="zh-CN" sz="{size}"{bold_attr} dirty="0"/>'
    return f"<a:p>{ppr}<a:r>{rpr}<a:t>{_esc(text)}</a:t></a:r></a:p>"


def _pptx_slide(title: str, bullets: Sequence[str], index: int) -> str:
    body = "".join(_pptx_text_paragraph(item, bullet=True) for item in bullets) or _pptx_text_paragraph("")
    return (
        XML_HEAD
        + f'<p:sld xmlns:a="{A_NS}" xmlns:r="{R_NS}" xmlns:p="{P_NS}"><p:cSld><p:spTree>'
        + _PPTX_GROUP
        + '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="838200" y="365125"/><a:ext cx="10515600" cy="1325563"/></a:xfrm></p:spPr>'
        f'<p:txBody><a:bodyPr/><a:lstStyle/>{_pptx_text_paragraph(title, size=3200, bold=True)}</p:txBody></p:sp>'
        '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Text 2"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="838200" y="1825625"/><a:ext cx="10515600" cy="4351338"/></a:xfrm></p:spPr>'
        f'<p:txBody><a:bodyPr/><a:lstStyle/>{body}</p:txBody></p:sp>'
        "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
    )


def create_pptx(path: Path, slides: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """生成 pptx。``slides`` 形如 ``[{"title": "...", "bullets": ["..."]}]``。"""

    slides = list(slides) or [{"title": "空白演示文稿", "bullets": []}]
    count = len(slides)

    overrides = [
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>',
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>',
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>',
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>',
    ]
    for index in range(1, count + 1):
        overrides.append(
            f'<Override PartName="/ppt/slides/slide{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        )
    content_types = (
        XML_HEAD
        + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + "".join(overrides)
        + "</Types>"
    )

    root_rels = (
        XML_HEAD
        + f'<Relationships xmlns="{PKG_REL_NS}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
        "</Relationships>"
    )

    slide_ids = "".join(
        f'<p:sldId id="{256 + index - 1}" r:id="rId{index + 1}"/>' for index in range(1, count + 1)
    )
    presentation = (
        XML_HEAD
        + f'<p:presentation xmlns:a="{A_NS}" xmlns:r="{R_NS}" xmlns:p="{P_NS}">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f"<p:sldIdLst>{slide_ids}</p:sldIdLst>"
        '<p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'
        "</p:presentation>"
    )

    pres_rels = (
        XML_HEAD
        + f'<Relationships xmlns="{PKG_REL_NS}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
        + "".join(
            f'<Relationship Id="rId{index + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{index}.xml"/>'
            for index in range(1, count + 1)
        )
        + "</Relationships>"
    )

    master_rels = (
        XML_HEAD
        + f'<Relationships xmlns="{PKG_REL_NS}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>'
        "</Relationships>"
    )

    layout_rels = (
        XML_HEAD
        + f'<Relationships xmlns="{PKG_REL_NS}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
        "</Relationships>"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/_rels/presentation.xml.rels", pres_rels)
        archive.writestr("ppt/slideMasters/slideMaster1.xml", _PPTX_MASTER)
        archive.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", master_rels)
        archive.writestr("ppt/slideLayouts/slideLayout1.xml", _PPTX_LAYOUT)
        archive.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", layout_rels)
        archive.writestr("ppt/theme/theme1.xml", _PPTX_THEME)
        for index, slide in enumerate(slides, start=1):
            title = str(slide.get("title") or f"第 {index} 页")
            bullets = [str(item) for item in (slide.get("bullets") or [])]
            archive.writestr(f"ppt/slides/slide{index}.xml", _pptx_slide(title, bullets, index))
            archive.writestr(
                f"ppt/slides/_rels/slide{index}.xml.rels",
                XML_HEAD
                + f'<Relationships xmlns="{PKG_REL_NS}">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
                "</Relationships>",
            )
    return {"kind": "pptx", "path": str(path), "slides": count, "size": path.stat().st_size}


def pptx_from_outline(path: Path, title: str, outline: str) -> Dict[str, Any]:
    """按大纲生成 pptx。

    语法：``;;`` 分页；每页 ``标题|要点1|要点2``。
    """

    slides: List[Dict[str, Any]] = []
    chunks = [chunk.strip() for chunk in outline.replace("\\n", "\n").split(";;") if chunk.strip()]
    if not chunks:
        chunks = [title or "空白演示文稿"]
    for chunk in chunks:
        parts = [part.strip() for part in chunk.replace("\n", "|").split("|") if part.strip()]
        if not parts:
            continue
        slides.append({"title": parts[0], "bullets": parts[1:]})
    if title and (not slides or slides[0]["title"] != title):
        slides.insert(0, {"title": title, "bullets": []})
    return create_pptx(path, slides)


# ---------------------------------------------------------------------------
# 生成：zip
# ---------------------------------------------------------------------------


def create_zip(path: Path, sources: Sequence[Path], *, base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """把若干文件/目录打包成 zip。"""

    if not sources:
        raise OfficeError("没有要压缩的文件")
    path.parent.mkdir(parents=True, exist_ok=True)
    added = 0
    total = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for source in sources:
            if source.is_dir():
                for child in sorted(source.rglob("*")):
                    if not child.is_file():
                        continue
                    arcname = child.relative_to(source.parent if base_dir is None else base_dir)
                    archive.write(child, str(arcname).replace("\\", "/"))
                    added += 1
                    total += child.stat().st_size
            elif source.is_file():
                arcname = source.name if base_dir is None else str(source.relative_to(base_dir))
                archive.write(source, arcname.replace("\\", "/"))
                added += 1
                total += source.stat().st_size
            else:
                raise OfficeError(f"路径不存在：{source}")
    return {"kind": "zip", "path": str(path), "entries": added, "source_bytes": total, "size": path.stat().st_size}


def extract_zip(path: Path, dest: Path) -> Dict[str, Any]:
    """解压 zip 到目录（防目录穿越）。"""

    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    extracted: List[str] = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(root)):
                raise OfficeError(f"压缩包包含非法路径：{info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            extracted.append(info.filename)
    return {"kind": "zip", "path": str(path), "dest": str(dest), "entries": len(extracted)}


# ---------------------------------------------------------------------------
# 预览
# ---------------------------------------------------------------------------


def preview(result: Dict[str, Any], limit: int = 1500) -> str:
    """把 read_any 的结果压成一段可读文本。"""

    text = (result.get("text") or "").strip()
    if not text:
        text = "（没有可显示的文本内容）"
    if len(text) > limit:
        text = text[:limit] + f"\n……（共 {len(text)} 字，已截断）"
    meta = result.get("meta") or {}
    extra = ""
    if result.get("kind") == "xlsx":
        extra = f"\n工作表 {meta.get('sheets', 0)} 个，共 {meta.get('rows', 0)} 行"
    elif result.get("kind") == "pptx":
        extra = f"\n共 {meta.get('slides', 0)} 页"
    elif result.get("kind") == "docx":
        extra = f"\n段落 {meta.get('paragraphs', 0)} 段，表格 {meta.get('tables', 0)} 个"
    elif result.get("kind") == "zip":
        extra = f"\n共 {meta.get('entries', 0)} 项"
    return f"{result.get('kind_label', '文件')}：{result.get('name', '')}{extra}\n\n{text}"
