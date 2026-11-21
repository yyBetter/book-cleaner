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

from book_cleaner.pipeline.data_clean import (
    _extract_kv_pairs,
    classify_record_dim,
    detect_batch_id,
    parse_numeric_value,
    sanitize_excel_text,
)


_HEADER_AUTO_MAP: dict[str, tuple[str, str]] = {
    "书名": ("builtin", "book_title"),
    "书名/卷宗": ("builtin", "book_title"),
    "作者": ("builtin", "author"),
    "isbn": ("builtin", "isbn"),
    "批次号": ("builtin", "batch_id"),
    "生产批号": ("builtin", "batch_id"),
    "批号": ("builtin", "batch_id"),
    "batch_id": ("builtin", "batch_id"),
    "记录维度": ("builtin", "record_dim"),
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


_SYSTEM_SKIP_SHEETS = {"异常待复核", "清洗汇总报告", "CatBoost_特征宽表", "CatBoost_字段契约"}
_DEFAULT_ALIASES_BY_ROLE_HINT: dict[str, list[str]] = {
    "批次号": ["批号", "生产批号", "Lot", "batch_id"],
    "记录日期": ["日期", "制定日期", "生产日期"],
    "温度": ["罐温", "温度(℃)", "Temp"],
    "压力": ["罐压", "压力(MPa)", "风压"],
    "溶氧": ["DO", "溶解氧", "溶氧(%)"],
    "pH": ["酸碱度", "pH值"],
}


def _infer_field_role_and_mode(sheet_name: str, hdr: str) -> str:
    h = hdr.strip()
    hl = h.lower()
    sname = sheet_name.lower()

    if hl in ("batch_id", "lot") or any(k in h for k in ("批次号", "生产批号", "批号")):
        return "meta_id"
    if hl.startswith("y_") or h.startswith("Y_") or any(
        k in h for k in ("效价", "总收率", "收率", "得率", "回收率", "目标y", "主产出", "最终纯度", "核心纯度", "质量判定", "合格判定")
    ):
        return "target_y"
    if any(k in h for k in ("日期", "时刻", "时间", "工龄")) or "date" in hl or "time" in hl:
        return "time_split"
    if any(k in h for k in ("正文", "备注", "说明", "内容", "交接", "异常描述", "原因")) or "sop" in hl or "note" in hl or "text" in hl:
        return "text_feature"
    if any(k in sname for k in ("结果", "终检", "放罐", "产出", "_y", "目标")) and not any(
        k in h for k in ("编号", "设备", "罐", "工位", "线", "批", "姓名", "人")
    ):
        return "target_y"
    if any(k in h for k in ("编号", "设备", "罐号", "工位", "产线", "生产线", "机台", "物料", "品名", "品种", "章节", "小节", "标题", "镜检", "菌检", "类型", "级别", "isbn", "author", "书名")):
        return "cat_feature"
    return "num_feature"


def read_workbook_sheets_from_xlsx(raw_bytes: bytes) -> list[dict[str, Any]]:
    """Parse ALL worksheets from an uploaded .xlsx workbook into Multi-Sheet Template Pack drawers."""
    if not raw_bytes:
        raise ValueError("上传的 Excel 文件为空")
    wb = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    sheets_out: list[dict[str, Any]] = []
    try:
        for idx, sname in enumerate(wb.sheetnames, start=1):
            clean_sname = str(sname).strip()
            if clean_sname in _SYSTEM_SKIP_SHEETS:
                continue
            ws = wb[sname]
            first_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
            headers = [str(cell).strip() for cell in first_row if cell is not None and str(cell).strip()]
            if not headers:
                continue

            fields: list[dict[str, Any]] = []
            used_keys: set[str] = set()
            roles_seen: set[str] = set()
            for f_idx, hdr in enumerate(headers, start=1):
                role = _infer_field_role_and_mode(clean_sname, hdr)
                roles_seen.add(role)
                base_key = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]+", "_", hdr).strip("_") or f"col_{f_idx}"
                if role == "meta_id" and "batch_id" not in used_keys:
                    base_key = "batch_id"
                key = base_key
                suffix = 2
                while key in used_keys:
                    key = f"{base_key}_{suffix}"
                    suffix += 1
                used_keys.add(key)

                aliases = list(_DEFAULT_ALIASES_BY_ROLE_HINT.get(hdr, []))
                fields.append(
                    {
                        "key": key,
                        "name": hdr,
                        "role": role,
                        "agg": "stats" if role == "num_feature" else ("join" if role == "text_feature" else "last"),
                        "aliases": aliases,
                    }
                )

            if "sop" in clean_sname.lower() or "规范" in clean_sname or "说明" in clean_sname or (
                roles_seen <= {"meta_id", "time_split", "cat_feature", "text_feature"} and "text_feature" in roles_seen
            ):
                mode = "text"
            elif any(k in clean_sname for k in ("过程", "巡检", "化验", "检测", "明细", "记录表", "台账")) or any(
                f["name"] in ("时刻", "记录时刻", "取样时刻", "工龄") for f in fields
            ):
                mode = "table"
            else:
                mode = "record"

            for f in fields:
                if f["role"] == "num_feature":
                    f["agg"] = "stats" if mode == "table" else "last"

            kw_parts = [p for p in re.split(r"[_\-\s/]+", clean_sname) if len(p) >= 2]
            for f in fields:
                if f["role"] in ("num_feature", "target_y", "cat_feature") and len(f["name"]) >= 2:
                    kw_parts.append(f["name"])
            keywords = list(dict.fromkeys(kw_parts))[:10]

            sheets_out.append(
                {
                    "sheet_id": f"sheet_{idx}",
                    "name": clean_sname[:31],
                    "mode": mode,
                    "match_keywords": keywords,
                    "fields": fields,
                }
            )
    finally:
        wb.close()

    if not sheets_out:
        raise ValueError("未在 Excel 的任意工作表第一行检测到非空表头列名")
    return sheets_out


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
    *,
    pages: list[dict[str, Any]] | None = None,
    page_assignments: dict[int, dict[str, Any]] | None = None,
) -> Path:
    """Export cleaned narrative entries, extracted tables, or Multi-Sheet Pack + CatBoost .xlsx."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if list(template.get("sheets") or []) and pages is not None:
        return export_multisheet_pack_workbook(
            book=book,
            template=template,
            pages=pages,
            page_assignments=page_assignments,
            out_path=out_path,
        )

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

    if bool(rules.get("catboost_mode", False)):
        _append_catboost_sheets(
            wb=wb,
            book=book,
            template=template,
            structured_rows=structured_rows,
            header_fill=header_fill,
            header_font=header_font,
            cell_border=cell_border,
            body_align=body_align,
        )

    wb.save(str(out_path))
    return out_path


_DATE_TOKEN_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_MAX_CATBOOST_NUM_DIMS = 16


def _clean_dim_slug(name: str) -> str:
    cleaned = re.sub(r"[\s/\\()（）\[\]{}:：]+", "_", (name or "").strip()).strip("_")
    return cleaned[:28] or "metric"


def _append_catboost_sheets(
    *,
    wb: Workbook,
    book: dict[str, Any],
    template: dict[str, Any],
    structured_rows: list[dict[str, Any]],
    header_fill: PatternFill,
    header_font: Font,
    cell_border: Border,
    body_align: Alignment,
) -> None:
    """Append `CatBoost_特征宽表` (1 row/batch, native floats) and `CatBoost_字段契约`."""
    columns = list(template.get("columns") or [])
    fallback_batch = sanitize_excel_text(book.get("isbn") or book.get("title") or "BATCH_001")
    fallback_batch = re.sub(r"\s+", "_", fallback_batch).strip("_")[:32] or "BATCH_001"
    default_asset = sanitize_excel_text(book.get("isbn") or book.get("title") or "ASSET_01")

    batches: dict[str, dict[str, Any]] = {}
    discovered_dims: list[str] = []
    table_headers_by_key: dict[tuple[int, int], list[str]] = {}

    def _ensure_batch(bid: str) -> dict[str, Any]:
        if bid not in batches:
            batches[bid] = {
                "batch_id": bid,
                "record_date": "",
                "asset_id": default_asset,
                "pages": set(),
                "tables": set(),
                "table_row_count": 0,
                "sop_entry_count": 0,
                "anomaly_note_count": 0,
                "sop_notes": [],
                "confs": [],
                "review_required": 0,
                "num_series": {},
            }
        return batches[bid]

    def _record_dim_val(bucket: dict[str, Any], raw_dim: str, raw_val: str) -> None:
        val = parse_numeric_value(raw_val)
        if val is None:
            return
        dim = _clean_dim_slug(raw_dim)
        if dim in ("页码", "page_no", "序号", "编号"):
            return
        if dim not in discovered_dims:
            if len(discovered_dims) >= _MAX_CATBOOST_NUM_DIMS:
                return
            discovered_dims.append(dim)
        bucket["num_series"].setdefault(dim, []).append(val)

    for r in structured_rows:
        kind = str(r.get("row_kind") or "ENTRY")
        data = r.get("data") or {}
        warns = r.get("warnings") or []
        page_no = int(r.get("page_no", 0))
        conf = float(r.get("ocr_confidence", 0.0))

        if kind == "TABLE":
            tbl_key = (page_no, int(r.get("table_index", 0)))
            bid = sanitize_excel_text(data.get("batch_id") or "") or fallback_batch
            bucket = _ensure_batch(bid)
            if page_no > 0:
                bucket["pages"].add(page_no)
            bucket["tables"].add(tbl_key)
            if conf > 0:
                bucket["confs"].append(conf)
            if warns:
                bucket["review_required"] = 1
                bucket["anomaly_note_count"] += 1

            cells = [sanitize_excel_text(c) for c in (data.get("cells") or [])]
            if data.get("is_header"):
                table_headers_by_key[tbl_key] = cells
            else:
                bucket["table_row_count"] += 1
                hdrs = table_headers_by_key.get(tbl_key, [])
                for c_idx, cell_val in enumerate(cells):
                    if not bucket["record_date"]:
                        dm = _DATE_TOKEN_RE.search(cell_val)
                        if dm:
                            bucket["record_date"] = dm.group(0)
                    col_name = hdrs[c_idx] if c_idx < len(hdrs) and hdrs[c_idx] else f"表列_{c_idx + 1}"
                    _record_dim_val(bucket, col_name, cell_val)
            continue

        # ENTRY row
        content_key = next((c.get("key") for c in columns if c.get("pattern") == "clean_content"), "clean_content")
        title_key = next((c.get("key") for c in columns if c.get("pattern") == "entry_title"), "entry_title")
        batch_key = next((c.get("key") for c in columns if c.get("pattern") == "batch_id"), "batch_id")
        dim_key = next((c.get("key") for c in columns if c.get("pattern") == "record_dim"), "record_dim")
        asset_key = next((c.get("key") for c in columns if c.get("pattern") == "isbn"), "asset_id")
        date_key = next((c.get("key") for c in columns if "date" in str(c.get("key", "")).lower()), "record_date")

        body_txt = sanitize_excel_text(data.get(content_key) or "")
        title_txt = sanitize_excel_text(data.get(title_key) or "")
        bid = sanitize_excel_text(data.get(batch_key) or "") or detect_batch_id(body_txt, "", fallback_batch)
        r_dim = sanitize_excel_text(data.get(dim_key) or "") or classify_record_dim(title_txt, body_txt)

        bucket = _ensure_batch(bid)
        if page_no > 0:
            bucket["pages"].add(page_no)
        if conf > 0:
            bucket["confs"].append(conf)
        if data.get(asset_key):
            bucket["asset_id"] = sanitize_excel_text(data.get(asset_key))
        if not bucket["record_date"]:
            raw_d = sanitize_excel_text(data.get(date_key) or "")
            dm = _DATE_TOKEN_RE.search(raw_d) or _DATE_TOKEN_RE.search(body_txt)
            if dm:
                bucket["record_date"] = dm.group(0)

        if r_dim == "SOP规范":
            bucket["sop_entry_count"] += 1
        if r_dim == "异常交接" or warns:
            bucket["anomaly_note_count"] += 1
        if warns:
            bucket["review_required"] = 1
        if body_txt:
            bucket["sop_notes"].append(body_txt)
        for k, v in _extract_kv_pairs(body_txt).items():
            _record_dim_val(bucket, k, v)

    # Sheet 5: CatBoost_特征宽表
    ws_cb = wb.create_sheet(title="CatBoost_特征宽表")
    base_cols = [
        "batch_id",
        "record_date",
        "asset_id",
        "page_count",
        "table_count",
        "table_row_count",
        "sop_entry_count",
        "anomaly_note_count",
    ]
    num_cols: list[tuple[str, str, str]] = []
    for dim in discovered_dims:
        num_cols.extend(
            [
                (f"{dim}_mean", dim, "mean"),
                (f"{dim}_min", dim, "min"),
                (f"{dim}_max", dim, "max"),
                (f"{dim}_last", dim, "last"),
            ]
        )
    tail_cols = ["sop_notes_text", "avg_ocr_conf", "review_required"]
    all_headers = base_cols + [h for h, _, _ in num_cols] + tail_cols
    ws_cb.append(all_headers)
    for cell in ws_cb[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.border = cell_border
        cell.alignment = Alignment(vertical="center", horizontal="left")

    for bid, b in batches.items():
        avg_conf = round(sum(b["confs"]) / len(b["confs"]), 4) if b["confs"] else None
        sop_text = sanitize_excel_text(" | ".join(b["sop_notes"]))[:2000]
        row_vals: list[Any] = [
            bid,
            b["record_date"],
            b["asset_id"],
            len(b["pages"]),
            len(b["tables"]),
            b["table_row_count"],
            b["sop_entry_count"],
            b["anomaly_note_count"],
        ]
        for _, dim, agg in num_cols:
            vals = b["num_series"].get(dim) or []
            if not vals:
                row_vals.append(None)
            elif agg == "mean":
                row_vals.append(round(sum(vals) / len(vals), 4))
            elif agg == "min":
                row_vals.append(round(min(vals), 4))
            elif agg == "max":
                row_vals.append(round(max(vals), 4))
            else:
                row_vals.append(round(vals[-1], 4))
        row_vals.extend([sop_text, avg_conf, int(b["review_required"])])
        ws_cb.append(row_vals)
        for cell in ws_cb[ws_cb.max_row]:
            cell.border = cell_border
            cell.alignment = body_align

    ws_cb.freeze_panes = "A2"
    if ws_cb.max_row >= 1 and ws_cb.max_column >= 1:
        ws_cb.auto_filter.ref = ws_cb.dimensions
    _auto_fit_columns(ws_cb, min_width=14, max_width=46)

    # Sheet 6: CatBoost_字段契约
    ws_schema = wb.create_sheet(title="CatBoost_字段契约")
    ws_schema.append(["column_name", "catboost_role", "dtype", "missing_policy", "description"])
    for cell in ws_schema[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.border = cell_border

    contract_rows = [
        ("batch_id", "meta_id", "string", "drop_row", "主建模粒度主键：一个批次一行；切分训练/验证集按时间+批次整批隔离，禁止同批拆散"),
        ("record_date", "time_split", "date", "forward_fill", "批次规范化日期 (YYYY-MM-DD)，用于严格时间序切分验证集"),
        ("asset_id", "cat_feature", "string", "empty_token", "设备/卷宗编号（传给 CatBoost cat_features）"),
        ("page_count", "num_feature", "int", "zero", "本批次关联总页数"),
        ("table_count", "num_feature", "int", "zero", "本批次提取表格数量"),
        ("table_row_count", "num_feature", "int", "zero", "本批次表格数据总行数"),
        ("sop_entry_count", "num_feature", "int", "zero", "SOP 工艺规范说明条目数"),
        ("anomaly_note_count", "num_feature", "int", "zero", "交接/异常说明或质检告警条数"),
    ]
    agg_cn = {"mean": "均值", "min": "最小值", "max": "最大值", "last": "末次记录值"}
    for h, dim, agg in num_cols:
        contract_rows.append(
            (h, "num_feature", "float", "native_nan", f"维度「{dim}」在批次内的{agg_cn[agg]}（已去单位转浮点，缺失保持空/NaN）")
        )
    contract_rows.extend(
        [
            ("sop_notes_text", "text_feature", "string", "empty_token", "拼接后的 SOP/过程/交接文本（传给 CatBoost text_features）"),
            ("avg_ocr_conf", "num_feature", "float", "native_nan", "平均 OCR 识别置信度（0~1 浮点，用于数据质量加权或过滤）"),
            ("review_required", "qa_gate", "int", "drop_row", "质量隔离门：0=可直接入训；1=含低置信或质检告警，禁止回流训练集"),
        ]
    )
    for item in contract_rows:
        ws_schema.append(list(item))
        for cell in ws_schema[ws_schema.max_row]:
            cell.border = cell_border
            cell.alignment = body_align

    ws_schema.freeze_panes = "A2"
    _auto_fit_columns(ws_schema, min_width=16, max_width=60)


def _dedup_sheet_title(raw_name: str, used: set[str], fallback: str) -> str:
    base = _safe_sheet_name(raw_name, fallback)
    cand = base
    idx = 2
    while cand in used:
        suffix = f"_{idx}"
        cand = f"{base[: max(1, 31 - len(suffix))]}{suffix}"
        idx += 1
    used.add(cand)
    return cand


def _excel_cell_val(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float)):
        return val
    s = sanitize_excel_text(val).strip()
    return s if s else None


def export_multisheet_pack_workbook(
    *,
    book: dict[str, Any],
    template: dict[str, Any],
    pages: list[dict[str, Any]],
    page_assignments: dict[int, dict[str, Any]] | None,
    out_path: Path,
) -> Path:
    """Export a Multi-Sheet Template Pack (.xlsx) with independent Sheet drawers + multi-y CatBoost wide table."""
    from book_cleaner.pipeline.sheet_matcher import match_book_to_template_pack

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    match_res = match_book_to_template_pack(book, pages, template, page_assignments)

    wb = Workbook()
    default_ws = wb.active
    wb.remove(default_ws)

    header_fill = PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=11)
    target_fill = PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid")
    target_font = Font(color="065F46", bold=True, size=11)
    warn_fill = PatternFill(start_color="FFF3CD", end_color="FFF3CD", fill_type="solid")
    thin_side = Side(style="thin", color="D0D7DE")
    cell_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    body_align = Alignment(vertical="top", wrap_text=True)

    used_titles: set[str] = set(_SYSTEM_SKIP_SHEETS)
    sheet_row_counts: list[tuple[str, int, int]] = []

    # 1. Write one worksheet per template drawer
    for s_idx, s in enumerate(match_res.get("sheets") or [], start=1):
        sheet_title = _dedup_sheet_title(str(s.get("name") or ""), used_titles, f"子表_{s_idx}")
        ws = wb.create_sheet(title=sheet_title)
        fields = list(s.get("fields") or [])
        headers = [sanitize_excel_text(f.get("name") or f.get("key")) for f in fields] + [
            "来源页码",
            "识别置信度",
            "清洗质检备注",
        ]
        target_indices = {idx for idx, f in enumerate(fields, start=1) if f.get("role") == "target_y"}
        ws.append(headers)
        for col_i, cell in enumerate(ws[1], start=1):
            is_y = col_i in target_indices
            cell.fill = target_fill if is_y else header_fill
            cell.font = target_font if is_y else header_font
            cell.border = cell_border
            cell.alignment = Alignment(vertical="center", horizontal="left")

        rows = list(s.get("rows") or [])
        for r in rows:
            meta = r.get("_meta") or {}
            warns = list(meta.get("warnings") or [])
            row_vals = [_excel_cell_val(r.get(f.get("key"))) for f in fields]
            row_vals.extend(
                [
                    int(meta.get("page_no", 0)),
                    round(float(meta.get("ocr_confidence", 0.0)), 4),
                    sanitize_excel_text("; ".join(warns)) if warns else None,
                ]
            )
            ws.append(row_vals)
            for cell in ws[ws.max_row]:
                cell.border = cell_border
                cell.alignment = body_align
                if warns:
                    cell.fill = warn_fill

        ws.freeze_panes = "A2"
        if ws.max_row >= 1 and ws.max_column >= 1:
            ws.auto_filter.ref = ws.dimensions
        _auto_fit_columns(ws, min_width=13, max_width=48)
        sheet_row_counts.append((sheet_title, len(s.get("assigned_pages") or []), len(rows)))

    # 2. Exception / unassigned sheet
    ws_warn = wb.create_sheet(title="异常待复核")
    ws_warn.append(["来源子表/归类", "页码", "告警原因", "内容摘要", "识别置信度"])
    for cell in ws_warn[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.border = cell_border
    for w in match_res.get("warning_rows") or []:
        ws_warn.append(
            [
                sanitize_excel_text(w.get("sheet_name") or ""),
                int(w.get("page_no", 0)),
                sanitize_excel_text("; ".join(w.get("warnings") or [])),
                sanitize_excel_text(w.get("summary") or ""),
                f"{float(w.get('ocr_confidence', 0.0)) * 100:.1f}%",
            ]
        )
        for cell in ws_warn[ws_warn.max_row]:
            cell.border = cell_border
            cell.alignment = body_align
            cell.fill = warn_fill
    ws_warn.freeze_panes = "A2"
    _auto_fit_columns(ws_warn, min_width=14, max_width=52)

    # 3. Summary sheet
    cov = match_res.get("coverage") or {}
    cb = match_res.get("catboost_dataset") or {}
    ws_sum = wb.create_sheet(title="清洗汇总报告")
    ws_sum.append(["指标项", "统计值"])
    for cell in ws_sum[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.border = cell_border
    summary_items = [
        ("工作集 / 书目", sanitize_excel_text(book.get("title") or "")),
        ("套用多表模板包", sanitize_excel_text(template.get("name") or "")),
        ("导出时间 (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("总页数", cov.get("total_pages", 0)),
        ("已成功分拣页数", cov.get("matched_pages", 0)),
        ("待确认归类页数", cov.get("unassigned_pages", 0)),
        ("暂不导出页数", cov.get("ignored_pages", 0)),
        ("纸面字段对齐率 (%)", cov.get("alignment_pct", 100.0)),
        ("沉淀批次数 (1批次=1行)", cov.get("total_batches", 0)),
        ("识别目标列 target_y", ", ".join(cb.get("target_y_cols") or []) or "无"),
    ]
    for title_s, p_cnt, r_cnt in sheet_row_counts:
        summary_items.append((f"子表「{title_s}」", f"{p_cnt} 页 · {r_cnt} 行"))
    for k, v in summary_items:
        ws_sum.append([k, v])
        for cell in ws_sum[ws_sum.max_row]:
            cell.border = cell_border
            cell.alignment = body_align
    _auto_fit_columns(ws_sum, min_width=18, max_width=64)

    # 4. CatBoost feature wide table + field schema contract
    rules = template.get("rules") or {}
    if bool(rules.get("catboost_mode", True)):
        ws_cb = wb.create_sheet(title="CatBoost_特征宽表")
        cb_headers = list(cb.get("headers") or ["batch_id", "record_date", "avg_ocr_conf", "review_required"])
        y_set = set(cb.get("target_y_cols") or [])
        ws_cb.append(cb_headers)
        for col_i, cell in enumerate(ws_cb[1]):
            is_y = cb_headers[col_i] in y_set
            cell.fill = target_fill if is_y else header_fill
            cell.font = target_font if is_y else header_font
            cell.border = cell_border
            cell.alignment = Alignment(vertical="center", horizontal="left")

        for rec in cb.get("rows") or []:
            ws_cb.append([_excel_cell_val(rec.get(h)) for h in cb_headers])
            for cell in ws_cb[ws_cb.max_row]:
                cell.border = cell_border
                cell.alignment = body_align
        ws_cb.freeze_panes = "A2"
        if ws_cb.max_row >= 1 and ws_cb.max_column >= 1:
            ws_cb.auto_filter.ref = ws_cb.dimensions
        _auto_fit_columns(ws_cb, min_width=14, max_width=46)

        ws_schema = wb.create_sheet(title="CatBoost_字段契约")
        ws_schema.append(["column_name", "catboost_role", "dtype", "source_sheet", "missing_policy", "description"])
        for cell in ws_schema[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.border = cell_border
        for spec in cb.get("schema_rows") or []:
            is_y = spec.get("catboost_role") == "target_y"
            ws_schema.append(
                [
                    spec.get("column_name", ""),
                    spec.get("catboost_role", ""),
                    spec.get("dtype", ""),
                    spec.get("source_sheet", ""),
                    spec.get("missing_policy", ""),
                    spec.get("description", ""),
                ]
            )
            for cell in ws_schema[ws_schema.max_row]:
                cell.border = cell_border
                cell.alignment = body_align
                if is_y:
                    cell.fill = target_fill
                    cell.font = Font(color="065F46", bold=True, size=10)
        ws_schema.freeze_panes = "A2"
        _auto_fit_columns(ws_schema, min_width=16, max_width=58)

    wb.save(str(out_path))
    return out_path
