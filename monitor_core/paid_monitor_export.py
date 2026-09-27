"""Small, dependency-free XLSX export for one paid-monitor data day."""

from __future__ import annotations

import io
import re
import zipfile
from html import escape
from typing import Any


_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _text(value: object) -> str:
    return _INVALID_XML.sub("", str(value or ""))[:32767]


def _column_name(number: int) -> str:
    output = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        output = chr(65 + remainder) + output
    return output


def _cell(row: int, column: int, value: object, style: int = 0) -> str:
    reference = f"{_column_name(column)}{row}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{reference}" s="{style}"><v>{value}</v></c>'
    return (
        f'<c r="{reference}" s="{style}" t="inlineStr"><is><t xml:space="preserve">'
        f"{escape(_text(value))}</t></is></c>"
    )


def _sheet(
    title: str, subtitle: str, headers: list[str], rows: list[list[object]], widths: list[int],
) -> str:
    column_xml = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, 1)
    )
    sheet_rows = [
        f'<row r="1" ht="28" customHeight="1">{_cell(1, 1, title, 2)}</row>',
        f'<row r="2" ht="20" customHeight="1">{_cell(2, 1, subtitle, 3)}</row>',
        '<row r="3"/>',
        '<row r="4" ht="24" customHeight="1">'
        + "".join(_cell(4, index, value, 1) for index, value in enumerate(headers, 1))
        + "</row>",
    ]
    for row_number, values in enumerate(rows, 5):
        sheet_rows.append(
            f'<row r="{row_number}" ht="36" customHeight="1">'
            + "".join(
                _cell(row_number, index, value, 4 if index in {3, 9, 15, 16, 17} else 0)
                for index, value in enumerate(values, 1)
            )
            + "</row>"
        )
    last_column = _column_name(len(headers))
    last_row = max(4, len(rows) + 4)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_column}{last_row}"/><sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="4" topLeftCell="A5" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews><sheetFormatPr defaultRowHeight="18"/>'
        f'<cols>{column_xml}</cols><sheetData>{"".join(sheet_rows)}</sheetData>'
        f'<mergeCells count="2"><mergeCell ref="A1:{last_column}1"/><mergeCell ref="A2:{last_column}2"/></mergeCells>'
        f'<autoFilter ref="A4:{last_column}{last_row}"/>'
        '<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>'
        '</worksheet>'
    )


def build_paid_monitor_xlsx(
    monitor: dict[str, Any], run_date: str, answers: list[dict[str, Any]],
) -> bytes:
    """Return a valid Excel workbook containing answer and source audit rows."""
    model_names = {"doubao": "豆包", "deepseek": "DeepSeek"}
    ordered = sorted(
        (item for item in answers if str(item.get("date") or "") == run_date),
        key=lambda item: (
            str(item.get("question") or ""), str(item.get("model") or ""),
            int(item.get("round") or 0),
        ),
    )
    answer_headers = [
        "数据日期", "模型", "监控问题", "轮次", "是否推荐", "是否提及", "推荐排名",
        "证据状态", "回答正文", "正文字数", "正文完整", "信源数量", "应有信源",
        "信源完整", "分析摘要", "情感", "识别品牌",
    ]
    answer_rows: list[list[object]] = []
    source_rows: list[list[object]] = []
    for item in ordered:
        analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        sources = item.get("sources") if isinstance(item.get("sources"), list) else []
        model = str(item.get("model") or "")
        answer_rows.append([
            run_date, model_names.get(model, model), item.get("question"),
            int(item.get("round") or 0), "是" if item.get("recommended") else "否",
            "是" if item.get("mentioned") else "否", item.get("rank") or "",
            item.get("evidence_state"), item.get("answer"), int(item.get("body_length") or 0),
            "是" if item.get("body_capture_complete") else "否", len(sources),
            int(item.get("expected_source_count") or 0),
            "是" if item.get("source_capture_complete") else "否",
            analysis.get("summary"), analysis.get("sentiment"),
            "、".join(str(value) for value in analysis.get("brands") or []),
        ])
        for index, source in enumerate(sources, 1):
            if not isinstance(source, dict):
                continue
            source_rows.append([
                run_date, model_names.get(model, model), item.get("question"),
                int(item.get("round") or 0), index, source.get("title"),
                source.get("url") or source.get("href"),
            ])

    brand = _text(monitor.get("brand_name") or "专题监控")
    subtitle = f"{run_date} · {len(answer_rows)} 条回答 · {len(source_rows)} 条信源"
    answer_sheet = _sheet(
        f"{brand} 每日监控数据", subtitle, answer_headers, answer_rows,
        [13, 13, 42, 8, 11, 11, 11, 13, 80, 11, 11, 11, 11, 11, 38, 12, 28],
    )
    source_sheet = _sheet(
        f"{brand} 每日信源数据", subtitle,
        ["数据日期", "模型", "监控问题", "轮次", "序号", "信源标题", "信源链接"],
        source_rows, [13, 13, 42, 8, 8, 48, 80],
    )
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>'''
    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''
    workbook = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="每日回答" sheetId="1" r:id="rId1"/><sheet name="信源链接" sheetId="2" r:id="rId2"/></sheets></workbook>'''
    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="3"><font><sz val="10"/><name val="Microsoft YaHei"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="10"/><name val="Microsoft YaHei"/></font><font><b/><color rgb="FF17233C"/><sz val="15"/><name val="Microsoft YaHei"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF316CF4"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="2"><border/><border><bottom style="thin"><color rgb="FFDDE5F1"/></bottom></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"><alignment vertical="center"/></xf><xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles)
        archive.writestr("xl/worksheets/sheet1.xml", answer_sheet)
        archive.writestr("xl/worksheets/sheet2.xml", source_sheet)
    return output.getvalue()
