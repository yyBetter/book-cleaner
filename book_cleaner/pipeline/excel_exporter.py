"""Multi-sheet Excel (.xlsx) exporter and existing workbook header template importer."""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from book_cleaner.pipeline.data_clean import sanitize_excel_text


_HEADER_AUTO_MAP: dict[str, tuple[str, str]] = {
    "书名": ("builtin", "book_title"),
    "书名/卷宗": ("builtin", "book_title"),
    "作者": ("builtin", "author"),
    "isbn": ("builtin", "isbn"),
    "页码": ("builtin", "page_no"),
    "章节": ("builtin", "chapter"),
    "所属章节": ("builtin", "chapter"),
    "标题": ("builtin", "entry_title"),
    "条目标题": ("builtin", "entry_title"),
    "正文": ("builtin", "clean_content"),
    "内容": ("builtin", "clean_content"),
    "清洗后正文": ("builtin", "clean_content"),
    "日期": ("regex", r"\b\d{4}-\d{2}-\d{2}\b"),
    "规范化日期": ("regex", r"\b\d{4}-\d{2}-\d{2}\b"),
    "置信度": ("builtin", "ocr_confidence"),
    "识别置信度": ("builtin", "ocr_confidence"),
    "状态": ("builtin", "qa_status"),
    "质检状态": ("builtin", "qa_status"),
    "来源文件": ("builtin", "source_ref"),
}


def _safe_sheet_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", "_", (name or "").strip())
    return (cleaned or fallback)[:31]


def read_headers_from_xlsx(raw_bytes: bytes) -> list[dict[str, Any]]:
    """Read row-1 headers from an uploaded .xlsx file and auto-map known column rules."""
    if not raw_bytes:
        raise ValueError("上传的 Excel 文件为空")
    wb = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    try:
        ws = wb.active
        first_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
    finally:
        wb.close()

    headers = [str(cell).strip() for cell in first_row if cell is not None and str(cell).strip()]
    if not headers:
        raise ValueError("未在 Excel 第一行检测到非空表头列名")

    columns: list[dict[str, Any]] = []
    used_keys: set[str] = set()
    for idx, hdr in enumerate(headers, start=1):
        low = hdr.lower()
        mapped = _HEADER_AUTO_MAP.get(hdr) or _HEADER_AUTO_MAP.get(low)
        if mapped:
            src_type, pattern = mapped
        else:
            src_type, pattern = ("kv", hdr)

        base_key = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]+", "_", hdr).strip("_") or f"col_{idx}"
        key = base_key
        suffix = 2
        while key in used_keys:
            key = f"{base_key}_{suffix}"
            suffix += 1
        used_keys.add(key)

        columns.append(
            {
                "key": key,
                "header": hdr,
                "source_type": src_type,
                "pattern": pattern,
                "required": pattern == "clean_content",
            }
        )
    return columns


def _auto_fit_columns(ws, min_width: int = 12, max_width: int = 52) -> None:
    for col_idx in range(1, ws.max_column + 1):
        col_letter = get_column_letter(col_idx)
        max_len = min_width
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 200), min_col=col_idx, max_col=col_idx):
            val = row[0].value
            if val is None:
                continue
            txt = str(val)
            first_line = txt.splitlines()[0] if txt else ""
            # CJK characters display roughly 2x ASCII width
            est = sum(2 if ord(ch) > 127 else 1 for ch in first_line[:60]) + 3
            if est > max_len:
                max_len = est
        ws.column_dimensions[col_letter].width = min(max_width, max(min_width, max_len))


def export_excel_workbook(
    book: dict[str, Any],
    template: dict[str, Any],
    structured_rows: list[dict[str, Any]],
    summary: dict[str, Any],
    out_path: Path,
) -> Path:
    """Export cleaned narrative entries, extracted tables, review warnings, and report to 4-sheet .xlsx."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    header_fill = PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=11)
    subhdr_fill = PatternFill(start_color="DCE6F1", end_color="DCE6F1", fill_type="solid")
    subhdr_font = Font(bold=True, size=10)
    warn_fill = PatternFill(start_color="FFF3CD", end_color="FFF3CD", fill_type="solid")
    thin_side = Side(style="thin", color="D0D7DE")
    cell_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    body_align = Alignment(vertical="top", wrap_text=True)

    columns = list(template.get("columns") or [])
    entry_rows = [r for r in structured_rows if r.get("row_kind") == "ENTRY"]
    table_rows = [r for r in structured_rows if r.get("row_kind") == "TABLE"]

    # ------------------------------------------------------------------
    # Sheet 1: Structured Narrative Entries
    # ------------------------------------------------------------------
    ws_entry = wb.active
    ws_entry.title = _safe_sheet_name(str(template.get("entry_sheet_name", "")), "正文结构化条目")
    entry_headers = [sanitize_excel_text(c.get("header") or c.get("key")) for c in columns] + ["清洗质检备注"]
    ws_entry.append(entry_headers)
    for cell in ws_entry[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.border = cell_border
        cell.alignment = Alignment(vertical="center", horizontal="left")

    for r in entry_rows:
        data = r.get("data") or {}
        warns = r.get("warnings") or []
        vals = [sanitize_excel_text(data.get(c.get("key") or c.get("header"), "")) for c in columns]
        vals.append(sanitize_excel_text("; ".join(warns)))
        ws_entry.append(vals)
        cur_row = ws_entry[ws_entry.max_row]
        for cell in cur_row:
            cell.border = cell_border
            cell.alignment = body_align
            if warns:
                cell.fill = warn_fill

    ws_entry.freeze_panes = "A2"
    if ws_entry.max_row >= 1 and ws_entry.max_column >= 1:
        ws_entry.auto_filter.ref = ws_entry.dimensions
    _auto_fit_columns(ws_entry)

    # ------------------------------------------------------------------
    # Sheet 2: Extracted Tables Detail
    # ------------------------------------------------------------------
    ws_tbl = wb.create_sheet(
        title=_safe_sheet_name(str(template.get("table_sheet_name", "")), "提取表格明细")
    )
    max_tbl_cols = max(
        [len((r.get("data") or {}).get("cells") or []) for r in table_rows] + [3]
    )
    tbl_headers = ["来源页码", "表格序号", "行类型"] + [f"列{i}" for i in range(1, max_tbl_cols + 1)] + ["清洗质检备注"]
    ws_tbl.append(tbl_headers)
    for cell in ws_tbl[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.border = cell_border
        cell.alignment = Alignment(vertical="center")

    for r in table_rows:
        data = r.get("data") or {}
        is_hdr = bool(data.get("is_header"))
        cells = [sanitize_excel_text(c) for c in (data.get("cells") or [])]
        cells += [""] * (max_tbl_cols - len(cells))
        warns = r.get("warnings") or []
        row_vals = [
            int(r.get("page_no", 0)),
            int(r.get("table_index", 0)),
            "表头" if is_hdr else "数据",
            *cells,
            sanitize_excel_text("; ".join(warns)),
        ]
        ws_tbl.append(row_vals)
        cur_row = ws_tbl[ws_tbl.max_row]
        for cell in cur_row:
            cell.border = cell_border
            cell.alignment = body_align
            if warns:
                cell.fill = warn_fill
            elif is_hdr:
                cell.fill = subhdr_fill
                cell.font = subhdr_font

    ws_tbl.freeze_panes = "A2"
    if ws_tbl.max_row >= 1 and ws_tbl.max_column >= 1:
        ws_tbl.auto_filter.ref = ws_tbl.dimensions
    _auto_fit_columns(ws_tbl)

    # ------------------------------------------------------------------
    # Sheet 3: Anomalies / Review Queue
    # ------------------------------------------------------------------
    ws_warn = wb.create_sheet(title="异常待复核")
    ws_warn.append(["来源类型", "页码", "告警原因", "摘要", "完整内容", "识别置信度"])
    for cell in ws_warn[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.border = cell_border

    for r in structured_rows:
        warns = r.get("warnings") or []
        if not warns:
            continue
        kind = str(r.get("row_kind", "ENTRY"))
        data = r.get("data") or {}
        if kind == "TABLE":
            full_txt = " | ".join(sanitize_excel_text(c) for c in (data.get("cells") or []))
            summary_txt = full_txt[:40]
        else:
            content_key = next(
                (c.get("key") for c in columns if c.get("pattern") == "clean_content"),
                "clean_content",
            )
            title_key = next(
                (c.get("key") for c in columns if c.get("pattern") == "entry_title"),
                "entry_title",
            )
            full_txt = sanitize_excel_text(
                data.get(content_key) or " | ".join(str(v) for v in data.values() if v)
            )
            summary_txt = sanitize_excel_text(data.get(title_key) or full_txt[:40])
        ws_warn.append(
            [
                "表格行" if kind == "TABLE" else "正文条目",
                int(r.get("page_no", 0)),
                sanitize_excel_text("; ".join(warns)),
                summary_txt,
                full_txt,
                f"{float(r.get('ocr_confidence', 0.0)) * 100:.1f}%",
            ]
        )
        for cell in ws_warn[ws_warn.max_row]:
            cell.border = cell_border
            cell.alignment = body_align
            cell.fill = warn_fill

    ws_warn.freeze_panes = "A2"
    _auto_fit_columns(ws_warn)

    # ------------------------------------------------------------------
    # Sheet 4: Summary Report
    # ------------------------------------------------------------------
    ws_sum = wb.create_sheet(title="清洗汇总报告")
    ws_sum.append(["指标项", "统计值"])
    for cell in ws_sum[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.border = cell_border

    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rules = template.get("rules") or {}
    summary_pairs = [
        ("书目名称", sanitize_excel_text(book.get("title", ""))),
        ("作者", sanitize_excel_text(book.get("author", ""))),
        ("采用清洗模板", sanitize_excel_text(template.get("name", ""))),
        ("导出时间 (UTC)", exported_at),
        ("参与清洗总页数", summary.get("pages_processed", 0)),
        ("正文结构化条目数", summary.get("entries", len(entry_rows))),
        ("识别独立表格数", summary.get("distinct_tables", 0)),
        ("提取表格总行数", summary.get("table_rows", len(table_rows))),
        ("自动去重过滤条数", summary.get("dedup_removed", 0)),
        ("异常待复核行总数", summary.get("warning_rows", 0)),
        ("质检通过率 (%)", summary.get("pass_rate_pct", 100.0)),
        ("去除重复页眉/页脚", sanitize_excel_text(", ".join(summary.get("running_headers_stripped") or []) or "无")),
        ("全角转半角", "开启" if rules.get("normalize_fullwidth", True) else "关闭"),
        ("日期统一 YYYY-MM-DD", "开启" if rules.get("normalize_dates", True) else "关闭"),
        ("低置信标黄阈值", rules.get("min_confidence_warn", 0.82)),
    ]
    for k, v in summary_pairs:
        ws_sum.append([k, v])
        for cell in ws_sum[ws_sum.max_row]:
            cell.border = cell_border
            cell.alignment = body_align

    _auto_fit_columns(ws_sum, min_width=18, max_width=64)

    wb.save(str(out_path))
    return out_path
