"""带样式的 xlsx 输出（纯标准库）。

office_io.create_xlsx 只写最朴素的表格；这里补上「改电子表格格式」需要的
字体、填充、边框、数字格式、列宽、冻结窗格与自动筛选。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

# 自定义数字格式编号（>=164 为自定义段）
_NUM_FMTS = {
    "0.00": 164,
    "0.0000": 165,
    "#,##0.00": 166,
    "0": 167,
    "0.0": 168,
    "0.00%": 10,  # 内置百分比
    "0%": 9,
}

_STYLE_DEFAULT = 0
_STYLE_HEADER = 1
_STYLE_TEXT = 2
_STYLE_CENTER = 3
_STYLE_NUM2 = 4
_STYLE_NUM4 = 5
_STYLE_INT = 6
_STYLE_MONEY = 7
_STYLE_PERCENT = 8
_STYLE_WRAP = 9

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{s}" xmlns:r="{r}"><sheets><sheet name="{name}" sheetId="1" r:id="rId1"/></sheets></workbook>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{s}">
<numFmts count="5">
<numFmt numFmtId="164" formatCode="0.00"/>
<numFmt numFmtId="165" formatCode="0.0000"/>
<numFmt numFmtId="166" formatCode="#,##0.00"/>
<numFmt numFmtId="167" formatCode="0"/>
<numFmt numFmtId="168" formatCode="0.0"/>
</numFmts>
<fonts count="3">
<font><sz val="11"/><color theme="1"/><name val="等线"/><family val="2"/></font>
<font><b/><sz val="11"/><color theme="1"/><name val="等线"/><family val="2"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="等线"/><family val="2"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF{fill}"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border>
<left style="thin"><color rgb="FFBFBFBF"/></left>
<right style="thin"><color rgb="FFBFBFBF"/></right>
<top style="thin"><color rgb="FFBFBFBF"/></top>
<bottom style="thin"><color rgb="FFBFBFBF"/></bottom>
<diagonal/>
</border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="10">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>
<xf numFmtId="165" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>
<xf numFmtId="167" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>
<xf numFmtId="166" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>
<xf numFmtId="10" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="常规" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _esc(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _col_letter(index: int) -> str:
    letters = ""
    value = index + 1
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _style_for(value: Any, fmt: str, align: str) -> int:
    """按列格式与对齐方式选 cellXfs 索引。"""

    if isinstance(value, bool):
        return _STYLE_CENTER if align == "center" else _STYLE_TEXT
    if isinstance(value, (int, float)):
        if fmt in ("0.00",):
            return _STYLE_NUM2
        if fmt in ("0.0000",):
            return _STYLE_NUM4
        if fmt in ("#,##0.00",):
            return _STYLE_MONEY
        if fmt in ("0.00%", "0%"):
            return _STYLE_PERCENT
        if fmt in ("0", "0.0"):
            return _STYLE_INT
        return _STYLE_TEXT
    if align == "center":
        return _STYLE_CENTER
    if align == "wrap":
        return _STYLE_WRAP
    return _STYLE_TEXT


def _cell_xml(ref: str, value: Any, style: int) -> str:
    if value is None or value == "":
        return f'<c r="{ref}" s="{style}"/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" s="{style}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    text = str(value)
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{_esc(text)}</t></is></c>'


def write_styled_xlsx(
    path: Path,
    rows: Sequence[Sequence[Any]],
    *,
    sheet_name: str = "Sheet1",
    header: bool = True,
    header_fill: str = "4472C4",
    header_bold: bool = True,
    freeze_header: bool = True,
    autofilter: bool = True,
    borders: bool = True,
    col_widths: Optional[Dict[int, float]] = None,
    number_formats: Optional[Dict[int, str]] = None,
    align: str = "left",
    row_height: Optional[float] = None,
    wrap_columns: Sequence[int] = (),
) -> Dict[str, Any]:
    """写出带样式的单表 xlsx。

    ``col_widths`` / ``number_formats`` 以 0 起始的列号或列字母为键。
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [list(row) for row in rows]
    formats = dict(number_formats or {})

    def _resolve(mapping: Dict[Any, Any]) -> Dict[int, Any]:
        resolved: Dict[int, Any] = {}
        for key, value in mapping.items():
            if isinstance(key, int):
                resolved[key] = value
            elif isinstance(key, str) and key.isalpha():
                index = 0
                for char in key.upper():
                    index = index * 26 + (ord(char) - 64)
                resolved[index - 1] = value
        return resolved

    widths = _resolve(col_widths or {})
    formats = _resolve(formats)

    body: List[str] = []
    for r_index, row in enumerate(rows):
        cells = []
        for c_index, value in enumerate(row):
            fmt = formats.get(c_index, "")
            cell_align = "wrap" if c_index in set(wrap_columns) else align
            style = _STYLE_HEADER if (header and r_index == 0) else _style_for(value, fmt, cell_align)
            cells.append(_cell_xml(f"{_col_letter(c_index)}{r_index + 1}", value, style))
        height = ""
        if header and r_index == 0 and row_height:
            height = f' ht="{row_height}" customHeight="1"'
        body.append(f'<row r="{r_index + 1}"{height}>{"".join(cells)}</row>')

    # 列宽：显式指定优先，否则按内容估算
    if not widths:
        for row in rows:
            for index, value in enumerate(row):
                width = len(str(value)) if value is not None else 0
                widths[index] = max(widths.get(index, 9.0), min(60.0, width * 1.6 + 2))
    cols_xml = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{round(float(w), 1)}" customWidth="1"/>'
        for i, w in sorted(widths.items())
    )

    pane = ""
    if header and freeze_header:
        pane = '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
    view = f'<sheetViews><sheetView workbookViewId="0">{pane}</sheetView></sheetViews>'

    last_col = _col_letter(max((len(row) for row in rows), default=1) - 1)
    filter_xml = f'<autoFilter ref="A1:{last_col}{max(len(rows), 1)}"/>' if (header and autofilter and rows) else ""

    sheet = (
        XML_HEAD
        + f'<worksheet xmlns="{S_NS}" xmlns:r="{R_NS}">'
        + view
        + (f"<cols>{cols_xml}</cols>" if cols_xml else "")
        + "<sheetData>"
        + "".join(body)
        + "</sheetData>"
        + filter_xml
        + "</worksheet>"
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", _WORKBOOK.format(s=S_NS, r=R_NS, name=_esc(sheet_name[:31] or "Sheet1")))
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/styles.xml", _STYLES.format(s=S_NS, fill=header_fill))
        archive.writestr("xl/worksheets/sheet1.xml", sheet)

    return {"kind": "xlsx", "path": str(path), "size": path.stat().st_size, "rows": len(rows)}
