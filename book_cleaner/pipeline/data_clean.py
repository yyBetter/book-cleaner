"""Stage 2: Mixed narrative & table data cleaning, header/footer removal, normalization, and schema mapping."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any


_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CHAPTER_RE = re.compile(
    r"^(?:##\s*)?(第[一二三四五六七八九十百千万零两0-9]+[章节回篇卷部节].{0,40}|Chapter\s+\d+.*)$",
    re.IGNORECASE,
)
_CN_DATE_RE = re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
_SEP_DATE_RE = re.compile(r"(?<!\d)(\d{4})[./](\d{1,2})[./](\d{1,2})(?!\d)")
_KV_PAIR_RE = re.compile(
    r"([^\n:：;；,，。、\s]{1,20})\s*[:：]\s*([^\n;；，。]+?)(?=(?:\s*[,，;；。]\s*[^\n:：;；,，。]+[:：])|[;；，。\n]|$)"
)
_BATCH_ID_RE = re.compile(
    r"(?:批次号?|生产批号|批号|Batch\s*ID|Batch|Lot)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_.-]{2,31})",
    re.IGNORECASE,
)
_NUMERIC_UNIT_RE = re.compile(
    r"^\s*([+-]?\d+(?:\.\d+)?)\s*(?:℃|°C|°F|%|MPa|kPa|Pa|mbar|bar|m³/h|m3/h|L/min|L/h|L|mL/min|mL|kg/h|kg|g/mL|mg/mL|μg/mL|ug/mL|IU/mL|U/mL|U/L|mmol/L|mol/L|g/L|mg/L|g|mg|t|吨|rpm|r/min|小时|分钟|天|hr|h|min|s|px|mm|cm|pH|mV)?\s*$",
    re.IGNORECASE,
)

_FULLWIDTH_MAP = {
    **{ord("０") + i: ord("0") + i for i in range(10)},
    **{ord("Ａ") + i: ord("A") + i for i in range(26)},
    **{ord("ａ") + i: ord("a") + i for i in range(26)},
    0x3000: ord(" "),
    ord("％"): ord("%"),
    ord("．"): ord("."),
    ord("－"): ord("-"),
    ord("／"): ord("/"),
    ord("＋"): ord("+"),
}


@dataclass(slots=True)
class CleaningRules:
    normalize_fullwidth: bool = True
    normalize_dates: bool = True
    strip_noise_symbols: bool = True
    remove_running_headers: bool = True
    deduplicate_entries: bool = True
    min_confidence_warn: float = 0.82
    catboost_mode: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CleaningRules":
        if not data:
            return cls()
        return cls(
            normalize_fullwidth=bool(data.get("normalize_fullwidth", True)),
            normalize_dates=bool(data.get("normalize_dates", True)),
            strip_noise_symbols=bool(data.get("strip_noise_symbols", True)),
            remove_running_headers=bool(data.get("remove_running_headers", True)),
            deduplicate_entries=bool(data.get("deduplicate_entries", True)),
            min_confidence_warn=float(data.get("min_confidence_warn", 0.82)),
            catboost_mode=bool(data.get("catboost_mode", False)),
        )


def parse_numeric_value(raw: str) -> float | None:
    """Parse a clean finite float from scalar text with optional industrial unit suffix."""
    if raw is None:
        return None
    s = str(raw).replace(",", "").strip()
    if not s:
        return None
    m = _NUMERIC_UNIT_RE.match(s)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    return val if math.isfinite(val) else None


def detect_batch_id(text: str, current_batch_id: str, fallback: str) -> str:
    """Extract batch identifier from page text, or carry forward current batch ID."""
    if text:
        m = _BATCH_ID_RE.search(text)
        if m:
            return m.group(1).strip()
    return current_batch_id or fallback


def classify_record_dim(title: str, content: str) -> str:
    """Classify a narrative block into SOP规范, 化验检测, 异常交接, or 过程说明."""
    combined = f"{title}\n{content}".lower()
    if any(k in combined for k in ("sop", "规程", "操作步骤", "标准操作", "注意事项", "工艺要求", "工艺规范", "门禁")):
        return "SOP规范"
    if any(k in combined for k in ("化验", "检测", "效价", "纯度", "镜检", "菌检", "采样", "ph", "od")):
        return "化验检测"
    if any(k in combined for k in ("异常", "交接", "故障", "报警", "偏差", "超标", "报废", "处理说明")):
        return "异常交接"
    return "过程说明"


def sanitize_excel_text(value: Any) -> str:
    """Ensure any cell string is safe for openpyxl / XML serialization."""
    if value is None:
        return ""
    text = str(value)
    return _ILLEGAL_XML_RE.sub("", text)


def _format_date_match(y: str, m: str, d: str, original: str) -> str:
    try:
        mi, di = int(m), int(d)
        if 1 <= mi <= 12 and 1 <= di <= 31:
            return f"{y}-{mi:02d}-{di:02d}"
    except ValueError:
        pass
    return original


def normalize_text(text: str, rules: CleaningRules | None = None) -> str:
    """Apply deterministic text cleanup: fullwidth->halfwidth, YYYY-MM-DD dates, noise stripping."""
    r = rules or CleaningRules()
    if not text:
        return ""
    out = text
    if r.strip_noise_symbols:
        out = out.replace("\ufffd", "")
        out = _ILLEGAL_XML_RE.sub("", out)
    if r.normalize_fullwidth:
        out = out.translate(_FULLWIDTH_MAP)
    if r.normalize_dates:
        out = _CN_DATE_RE.sub(
            lambda m: _format_date_match(m.group(1), m.group(2), m.group(3), m.group(0)), out
        )
        out = _SEP_DATE_RE.sub(
            lambda m: _format_date_match(m.group(1), m.group(2), m.group(3), m.group(0)), out
        )
    lines = [ln.rstrip() for ln in out.splitlines()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def detect_running_headers(pages: list[dict[str, Any]], rules: CleaningRules) -> set[str]:
    """Identify short repeating running headers/footers appearing across >= 30% of pages."""
    if not rules.remove_running_headers or len(pages) < 3:
        return set()

    counts: dict[str, int] = {}
    for p in pages:
        txt = normalize_text(str(p.get("clean_text") or p.get("ocr_text") or ""), rules)
        non_empty = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        if not non_empty:
            continue
        candidates = {non_empty[0]}
        if len(non_empty) > 1:
            candidates.add(non_empty[-1])
        for cand in candidates:
            if 1 <= len(cand) <= 40 and not cand.startswith("#") and not _CHAPTER_RE.match(cand):
                counts[cand] = counts.get(cand, 0) + 1

    threshold = max(2, math.ceil(len(pages) * 0.30))
    return {line for line, cnt in counts.items() if cnt >= threshold}


def _strip_running_lines(text: str, running_headers: set[str]) -> str:
    if not running_headers or not text:
        return text
    kept = [ln for ln in text.splitlines() if ln.strip() not in running_headers]
    return "\n".join(kept).strip()


# ----------------------------------------------------------------------
# Mixed Table & Narrative Body Separation
# ----------------------------------------------------------------------
def _extract_spatial_tables_from_blocks(
    blocks: list[dict[str, Any]],
    rules: CleaningRules,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Detect tabular grids directly from OCR bounding boxes."""
    if not blocks or len(blocks) < 4:
        return [], set()

    parsed: list[dict[str, Any]] = []
    for b in blocks:
        bbox = b.get("bbox") or []
        txt = normalize_text(str(b.get("text") or ""), rules)
        if not txt or len(bbox) < 4:
            continue
        xs = [float(pt[0]) for pt in bbox]
        ys = [float(pt[1]) for pt in bbox]
        parsed.append(
            {
                "text": txt,
                "raw_text": str(b.get("text") or "").strip(),
                "x0": min(xs),
                "x1": max(xs),
                "y0": min(ys),
                "y1": max(ys),
                "yc": 0.5 * (min(ys) + max(ys)),
                "h": max(4.0, max(ys) - min(ys)),
            }
        )

    if len(parsed) < 4:
        return [], set()

    parsed.sort(key=lambda item: (item["yc"], item["x0"]))
    med_h = sorted(item["h"] for item in parsed)[len(parsed) // 2]

    rows: list[list[dict[str, Any]]] = []
    for blk in parsed:
        placed = False
        for r in reversed(rows[-3:]):
            r_yc = sum(x["yc"] for x in r) / len(r)
            if abs(blk["yc"] - r_yc) <= 0.55 * med_h:
                r.append(blk)
                placed = True
                break
        if not placed:
            rows.append([blk])

    # Merge horizontally close boxes inside each visual row into logical cells
    row_cells: list[dict[str, Any]] = []
    for r in rows:
        r.sort(key=lambda x: x["x0"])
        merged_cells: list[dict[str, Any]] = []
        for cell in r:
            if merged_cells and (cell["x0"] - merged_cells[-1]["x1"]) <= med_h * 1.35:
                prev = merged_cells[-1]
                prev["text"] = f"{prev['text']} {cell['text']}".strip()
                prev["x1"] = max(prev["x1"], cell["x1"])
                prev["raw_items"].append(cell["raw_text"])
            else:
                merged_cells.append(
                    {
                        "text": cell["text"],
                        "x0": cell["x0"],
                        "x1": cell["x1"],
                        "raw_items": [cell["raw_text"]],
                    }
                )
        row_cells.append(
            {
                "cells": merged_cells,
                "y0": min(x["y0"] for x in r),
                "y1": max(x["y1"] for x in r),
            }
        )

    # Group contiguous multi-cell rows (>= 2 rows, >= 2 cells per row)
    tables: list[dict[str, Any]] = []
    consumed_raw_texts: set[str] = set()
    run: list[dict[str, Any]] = []

    def _flush_run(curr_run: list[dict[str, Any]]) -> None:
        if len(curr_run) < 2:
            return
        all_x0 = sorted(c["x0"] for row in curr_run for c in row["cells"])
        tol = max(24.0, med_h * 2.2)
        col_anchors: list[float] = []
        for x in all_x0:
            if not col_anchors or abs(x - col_anchors[-1]) > tol:
                col_anchors.append(x)
        if not (2 <= len(col_anchors) <= 12):
            return

        grid: list[list[str]] = []
        for row in curr_run:
            slots = [""] * len(col_anchors)
            for cell in row["cells"]:
                col_idx = min(range(len(col_anchors)), key=lambda i: abs(cell["x0"] - col_anchors[i]))
                slots[col_idx] = (f"{slots[col_idx]} {cell['text']}" if slots[col_idx] else cell["text"]).strip()
            if sum(1 for s in slots if s) >= 2:
                grid.append(slots)
                for cell in row["cells"]:
                    consumed_raw_texts.update(cell["raw_items"])
                    consumed_raw_texts.add(cell["text"])

        if len(grid) >= 2:
            tables.append({"headers": grid[0], "rows": grid[1:]})

    for rc in row_cells:
        if len(rc["cells"]) >= 2:
            if run and (rc["y0"] - run[-1]["y1"]) > med_h * 2.6:
                _flush_run(run)
                run = [rc]
            else:
                run.append(rc)
        else:
            _flush_run(run)
            run = []
    _flush_run(run)

    return tables, consumed_raw_texts


def _split_text_table_line(line: str) -> tuple[str, list[str]] | None:
    s = line.strip()
    if not s:
        return None
    # Markdown pipe table
    if "|" in s and s.count("|") >= 2:
        stripped = s.strip("|")
        cells = [c.strip() for c in stripped.split("|")]
        if len(cells) >= 2:
            # Skip markdown divider line like |---|---|
            if all(re.fullmatch(r"[:\-\s]+", c or "-") for c in cells):
                return ("pipe_sep", [])
            return ("pipe", cells)
    # Tab-separated table
    if "\t" in s:
        cells = [c.strip() for c in s.split("\t") if c.strip()]
        if len(cells) >= 2:
            return ("tsv", cells)
    # Multi-space aligned table (>= 2 spaces between columns)
    if re.search(r"\s{2,}", s):
        cells = [c.strip() for c in re.split(r"\s{2,}", s) if c.strip()]
        if 2 <= len(cells) <= 10:
            return ("spaced", cells)
    return None


def _extract_text_tables(text: str) -> tuple[list[dict[str, Any]], str]:
    """Extract Markdown pipe, TSV, or space-aligned tables from cleaned text and return remaining body."""
    lines = text.splitlines()
    tables: list[dict[str, Any]] = []
    kept_lines: list[str] = []

    i = 0
    while i < len(lines):
        parsed = _split_text_table_line(lines[i])
        if parsed is None or parsed[0] == "pipe_sep":
            kept_lines.append(lines[i])
            i += 1
            continue

        kind, first_cells = parsed
        run_rows: list[list[str]] = [first_cells]
        run_lines: list[str] = [lines[i]]
        j = i + 1
        while j < len(lines):
            nxt = _split_text_table_line(lines[j])
            if nxt is None:
                break
            nxt_kind, nxt_cells = nxt
            if kind == "pipe" and nxt_kind == "pipe_sep":
                run_lines.append(lines[j])
                j += 1
                continue
            if nxt_kind != kind:
                break
            if kind == "spaced" and abs(len(nxt_cells) - len(first_cells)) > 1:
                break
            run_rows.append(nxt_cells)
            run_lines.append(lines[j])
            j += 1

        if len(run_rows) >= 2:
            max_cols = max(len(r) for r in run_rows)
            norm_rows = [r + [""] * (max_cols - len(r)) for r in run_rows]
            tables.append({"headers": norm_rows[0], "rows": norm_rows[1:]})
            i = j
        else:
            kept_lines.append(lines[i])
            i += 1

    return tables, "\n".join(kept_lines).strip()


def extract_tables_and_body(
    page: dict[str, Any],
    rules: CleaningRules,
    running_headers: set[str],
) -> tuple[list[dict[str, Any]], str]:
    """Separate structured tables and clean narrative body for a single page."""
    raw_text = str(page.get("clean_text") or page.get("ocr_text") or "")
    norm_text = normalize_text(raw_text, rules)
    norm_text = _strip_running_lines(norm_text, running_headers)

    blocks = page.get("ocr_blocks") or []
    spatial_tables, consumed_tokens = _extract_spatial_tables_from_blocks(blocks, rules)
    text_tables, remaining_body = _extract_text_tables(norm_text)

    # Prefer text tables if both caught the exact same content, otherwise keep union cleanly
    seen_sigs: set[str] = set()
    final_tables: list[dict[str, Any]] = []
    for tbl in [*text_tables, *spatial_tables]:
        sig = "|".join(tbl["headers"]) + "||" + "|".join("|".join(r) for r in tbl["rows"])
        if sig not in seen_sigs:
            seen_sigs.add(sig)
            final_tables.append(tbl)

    # Remove residual lines purely composed of spatial-table consumed cells from remaining_body
    if consumed_tokens and remaining_body:
        body_lines: list[str] = []
        for ln in remaining_body.splitlines():
            s = ln.strip()
            if not s:
                body_lines.append("")
                continue
            parts = [p.strip() for p in re.split(r"\s{2,}", s) if p.strip()]
            if parts and all(p in consumed_tokens for p in parts) and len(parts) >= 2:
                continue
            body_lines.append(ln)
        remaining_body = re.sub(r"\n{3,}", "\n\n", "\n".join(body_lines)).strip()

    return final_tables, remaining_body


# ----------------------------------------------------------------------
# Custom Column Evaluation & Book-Wide Structured Cleaning
# ----------------------------------------------------------------------
def _extract_kv_pairs(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for m in _KV_PAIR_RE.finditer(text):
        k = m.group(1).strip()
        v = m.group(2).strip()
        if k and v and len(k) <= 24:
            pairs[k] = v
    return pairs


def _evaluate_column(
    col: dict[str, Any],
    *,
    book: dict[str, Any],
    page_no: int,
    chapter: str,
    entry_title: str,
    clean_content: str,
    ocr_confidence: float,
    qa_status: str,
    source_ref: str,
    kv_cache: dict[str, str],
    batch_id: str = "",
    record_dim: str = "过程说明",
) -> str:
    src_type = str(col.get("source_type", "builtin")).lower()
    pattern = str(col.get("pattern", "")).strip()

    if src_type == "builtin":
        mapping = {
            "book_title": book.get("title", ""),
            "author": book.get("author", ""),
            "isbn": book.get("isbn", ""),
            "page_no": str(page_no),
            "chapter": chapter,
            "entry_title": entry_title,
            "clean_content": clean_content,
            "ocr_confidence": f"{ocr_confidence * 100:.1f}%",
            "qa_status": qa_status,
            "source_ref": source_ref,
            "batch_id": batch_id,
            "record_dim": record_dim,
        }
        return sanitize_excel_text(mapping.get(pattern, ""))

    if src_type == "regex":
        if not pattern:
            return ""
        try:
            matches = re.findall(pattern, clean_content)
        except re.error:
            return ""
        uniq: list[str] = []
        for m in matches:
            val = " ".join(x for x in m if x).strip() if isinstance(m, tuple) else str(m).strip()
            if val and val not in uniq:
                uniq.append(val)
        return sanitize_excel_text(", ".join(uniq))

    if src_type == "kv":
        if not kv_cache:
            return ""
        if not pattern or pattern == "*":
            return sanitize_excel_text("; ".join(f"{k}: {v}" for k, v in kv_cache.items()))
        if pattern in kv_cache:
            return sanitize_excel_text(kv_cache[pattern])
        # Fuzzy match key containing pattern
        for k, v in kv_cache.items():
            if pattern in k:
                return sanitize_excel_text(v)
        return ""

    return ""


def _split_narrative_entries(body: str, initial_chapter: str) -> tuple[list[dict[str, str]], str]:
    """Split remaining non-table text into structured entries while tracking chapter headings."""
    if not body.strip():
        return [], initial_chapter

    chapter = initial_chapter
    pending_title = ""
    entries: list[dict[str, str]] = []

    blocks = [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]
    for blk in blocks:
        lines = [ln.strip() for ln in blk.splitlines() if ln.strip()]
        if not lines:
            continue

        # Pure heading block
        if len(lines) == 1 and (lines[0].startswith("## ") or lines[0].startswith("# ") or _CHAPTER_RE.match(lines[0])):
            heading_clean = lines[0].lstrip("#").strip()
            if _CHAPTER_RE.match(heading_clean):
                chapter = heading_clean
            pending_title = heading_clean
            continue

        # Block starts with heading line + body lines
        entry_title = pending_title
        content_lines = lines
        if lines[0].startswith("## ") or lines[0].startswith("# ") or _CHAPTER_RE.match(lines[0]):
            heading_clean = lines[0].lstrip("#").strip()
            if _CHAPTER_RE.match(heading_clean):
                chapter = heading_clean
            entry_title = heading_clean
            content_lines = lines[1:]

        pending_title = ""
        content_text = "\n".join(content_lines).strip()
        if not content_text:
            continue

        if not entry_title:
            first_line = content_lines[0]
            entry_title = first_line[:28] + ("..." if len(first_line) > 28 else "")

        entries.append(
            {
                "chapter": chapter,
                "entry_title": entry_title,
                "clean_content": content_text,
            }
        )

    return entries, chapter


def extract_book_structured_data(
    book: dict[str, Any],
    pages: list[dict[str, Any]],
    template: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run full Stage-2 cleaning, table/narrative split, dedup, and schema mapping across a book."""
    rules = CleaningRules.from_dict(template.get("rules"))
    columns = list(template.get("columns") or [])
    running_headers = detect_running_headers(pages, rules)

    rows_to_store: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    dedup_removed = 0
    distinct_tables = 0
    table_rows_count = 0
    entry_count = 0
    warning_rows = 0
    current_chapter = ""
    fallback_batch = sanitize_excel_text(book.get("isbn") or book.get("title") or "BATCH_001")
    fallback_batch = re.sub(r"\s+", "_", fallback_batch).strip("_")[:32] or "BATCH_001"
    current_batch_id = ""

    for page in pages:
        page_id = page.get("id")
        page_no = int(page.get("seq_no", 0))
        conf = float(page.get("ocr_confidence", 0.0))
        qa_flags = list(page.get("qa_flags") or [])
        source_ref = str(page.get("source_ref") or "")
        page_txt = normalize_text(str(page.get("clean_text") or page.get("ocr_text") or ""), rules)
        current_batch_id = detect_batch_id(page_txt, current_batch_id, fallback_batch)

        page_warnings: list[str] = []
        if 0.0 < conf < rules.min_confidence_warn:
            page_warnings.append(f"OCR低置信度({conf * 100:.1f}%)")
        elif conf == 0.0 and not (page.get("clean_text") or page.get("ocr_text")):
            page_warnings.append("无识别文本")
        if qa_flags:
            page_warnings.append(f"原页质检告警:{','.join(qa_flags)}")

        tables, narrative_body = extract_tables_and_body(page, rules, running_headers)

        # 1. Store extracted table rows
        for t_idx, tbl in enumerate(tables, start=1):
            distinct_tables += 1
            headers = [sanitize_excel_text(c) for c in tbl["headers"]]
            hdr_warnings = list(page_warnings)
            rows_to_store.append(
                {
                    "row_kind": "TABLE",
                    "page_id": page_id,
                    "page_no": page_no,
                    "table_index": t_idx,
                    "row_index": 0,
                    "data": {"is_header": True, "cells": headers, "batch_id": current_batch_id},
                    "warnings": hdr_warnings,
                    "row_hash": "",
                    "ocr_confidence": conf,
                }
            )
            table_rows_count += 1
            if hdr_warnings:
                warning_rows += 1

            for r_idx, row_cells in enumerate(tbl["rows"], start=1):
                clean_cells = [sanitize_excel_text(c) for c in row_cells]
                r_warnings = list(page_warnings)
                rows_to_store.append(
                    {
                        "row_kind": "TABLE",
                        "page_id": page_id,
                        "page_no": page_no,
                        "table_index": t_idx,
                        "row_index": r_idx,
                        "data": {"is_header": False, "cells": clean_cells, "batch_id": current_batch_id},
                        "warnings": r_warnings,
                        "row_hash": "",
                        "ocr_confidence": conf,
                    }
                )
                table_rows_count += 1
                if r_warnings:
                    warning_rows += 1

        # 2. Store structured narrative entries
        entries, current_chapter = _split_narrative_entries(narrative_body, current_chapter)
        for ent in entries:
            norm_sig = re.sub(r"\s+", " ", ent["clean_content"]).strip()
            r_hash = hashlib.sha256(norm_sig.encode("utf-8")).hexdigest()
            if rules.deduplicate_entries and r_hash in seen_hashes:
                dedup_removed += 1
                continue
            seen_hashes.add(r_hash)

            kv_cache = _extract_kv_pairs(ent["clean_content"])
            entry_batch_id = detect_batch_id(ent["clean_content"], current_batch_id, fallback_batch)
            record_dim = classify_record_dim(ent["entry_title"], ent["clean_content"])
            row_warnings = list(page_warnings)

            # Evaluate user columns (except qa_status which depends on row_warnings)
            row_data: dict[str, Any] = {}
            for col in columns:
                key = str(col.get("key") or col.get("header") or "col")
                if col.get("source_type") == "builtin" and col.get("pattern") == "qa_status":
                    continue
                val = _evaluate_column(
                    col,
                    book=book,
                    page_no=page_no,
                    chapter=ent["chapter"],
                    entry_title=ent["entry_title"],
                    clean_content=ent["clean_content"],
                    ocr_confidence=conf,
                    qa_status="",
                    source_ref=source_ref,
                    kv_cache=kv_cache,
                    batch_id=entry_batch_id,
                    record_dim=record_dim,
                )
                row_data[key] = val
                if col.get("required") and not val.strip():
                    row_warnings.append(f"必填列缺失:{col.get('header', key)}")

            qa_label = "需复核" if row_warnings else "通过"
            for col in columns:
                if col.get("source_type") == "builtin" and col.get("pattern") == "qa_status":
                    key = str(col.get("key") or col.get("header") or "qa_status")
                    row_data[key] = qa_label

            rows_to_store.append(
                {
                    "row_kind": "ENTRY",
                    "page_id": page_id,
                    "page_no": page_no,
                    "table_index": 0,
                    "row_index": entry_count + 1,
                    "data": row_data,
                    "warnings": row_warnings,
                    "row_hash": r_hash,
                    "ocr_confidence": conf,
                }
            )
            entry_count += 1
            if row_warnings:
                warning_rows += 1

    total_rows = entry_count + table_rows_count
    pass_rate = round(((total_rows - warning_rows) / float(total_rows)) * 100.0, 1) if total_rows else 100.0
    summary = {
        "book_id": book.get("id"),
        "book_title": book.get("title", ""),
        "template_id": template.get("id"),
        "template_name": template.get("name", ""),
        "pages_processed": len(pages),
        "entries": entry_count,
        "distinct_tables": distinct_tables,
        "table_rows": table_rows_count,
        "dedup_removed": dedup_removed,
        "warning_rows": warning_rows,
        "total_rows": total_rows,
        "pass_rate_pct": pass_rate,
        "running_headers_stripped": sorted(running_headers),
    }
    return rows_to_store, summary
