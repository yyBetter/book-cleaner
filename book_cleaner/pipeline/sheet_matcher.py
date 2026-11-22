"""Multi-Sheet template pack page router, column/alias matcher, and CatBoost multi-y assembler."""

from __future__ import annotations

import difflib
import math
import re
import statistics
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from book_cleaner.pipeline.pii_redactor import ConsistentPIIRedactor

from book_cleaner.pipeline.data_clean import (
    CleaningRules,
    _extract_kv_pairs,
    detect_batch_id,
    detect_running_headers,
    extract_tables_and_body,
    normalize_text,
    parse_numeric_value,
    sanitize_excel_text,
)


_UNIT_PAREN_RE = re.compile(r"[(\（][^\)\）]{1,18}[\)\）]")
_STRIP_DELIM_RE = re.compile(r"[\s_\-:：/\\|·,，.。]+")
_DATE_TOKEN_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def normalize_col_token(name: str) -> str:
    """Normalize a column header or alias by removing unit parentheses and delimiters."""
    s = str(name or "").strip()
    if not s:
        return ""
    no_unit = _UNIT_PAREN_RE.sub("", s)
    cleaned = _STRIP_DELIM_RE.sub("", no_unit).lower()
    if not cleaned:
        cleaned = _STRIP_DELIM_RE.sub("", s).lower()
    return cleaned


def match_column_to_fields(
    source_col: str,
    fields: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """Match a discovered source column/key to a Sheet's target field definition."""
    src_raw = str(source_col or "").strip()
    if not src_raw or not fields:
        return None, "unmatched"
    src_low = src_raw.lower()
    src_norm = normalize_col_token(src_raw)

    # 1. Exact name / key match
    for f in fields:
        f_name = str(f.get("name") or "").strip()
        f_key = str(f.get("key") or "").strip()
        if src_low in (f_name.lower(), f_key.lower()):
            return f, "matched_exact"
        if src_norm and src_norm in (normalize_col_token(f_name), normalize_col_token(f_key)):
            return f, "matched_exact"

    # 2. Learned or built-in alias match
    for f in fields:
        for alias in f.get("aliases") or []:
            a_str = str(alias or "").strip()
            if not a_str:
                continue
            if src_low == a_str.lower() or (src_norm and src_norm == normalize_col_token(a_str)):
                return f, "matched_alias"

    # 3. Fuzzy containment or SequenceMatcher similarity
    best_field: dict[str, Any] | None = None
    best_score = 0.0
    if len(src_norm) >= 2:
        for f in fields:
            candidates = [str(f.get("name") or ""), str(f.get("key") or ""), *[str(a) for a in (f.get("aliases") or [])]]
            for cand in candidates:
                cnorm = normalize_col_token(cand)
                if len(cnorm) < 2:
                    continue
                if src_norm in cnorm or cnorm in src_norm:
                    score = 0.85 * min(len(src_norm), len(cnorm)) / max(len(src_norm), len(cnorm)) + 0.15
                else:
                    score = difflib.SequenceMatcher(None, src_norm, cnorm).ratio()
                if score > best_score:
                    best_score = score
                    best_field = f
    if best_field is not None and best_score >= 0.72:
        return best_field, "matched_fuzzy"

    return None, "unmatched"


def _clean_title_snippet(text: str) -> str:
    for line in (text or "").splitlines():
        s = re.sub(r"^[#*\-\s>]+", "", line).strip()
        if s:
            return s[:48]
    return "空白/待识别页"


def _extract_page_signals(
    page: dict[str, Any],
    rules: CleaningRules,
    running_headers: set[str],
) -> dict[str, Any]:
    raw_txt = str(page.get("clean_text") or page.get("ocr_text") or "")
    norm_txt = normalize_text(raw_txt, rules)
    tables, narrative = extract_tables_and_body(page, rules, running_headers)
    kv_pairs = _extract_kv_pairs(norm_txt)

    # Also harvest 2-column form tables into kv_pairs so vertical key/value forms map cleanly
    table_headers: list[str] = []
    for tbl in tables:
        hdrs = [sanitize_excel_text(h).strip() for h in (tbl.get("headers") or []) if str(h).strip()]
        for h in hdrs:
            if h not in table_headers and len(h) <= 32:
                table_headers.append(h)
        if len(hdrs) == 2 and hdrs[0] not in ("项目", "指标", "参数", "名称", "字段"):
            # Treat 2-col table without generic headers or with data rows smoothly
            pass
        for row in tbl.get("rows") or []:
            if len(row) == 2:
                k = sanitize_excel_text(row[0]).strip().rstrip(":：")
                v = sanitize_excel_text(row[1]).strip()
                if k and len(k) <= 24 and not parse_numeric_value(k) and k not in kv_pairs:
                    kv_pairs[k] = v

    source_cols: list[str] = []
    for col in table_headers + list(kv_pairs.keys()):
        c = col.strip()
        if not c or len(c) > 28 or c in source_cols:
            continue
        if parse_numeric_value(c) is not None:
            continue
        source_cols.append(c)

    lines = [re.sub(r"^[#*\s]+", "", ln).strip() for ln in norm_txt.splitlines() if ln.strip()]
    top_text = "\n".join(lines[:3]).lower()
    full_low = norm_txt.lower()
    date_m = _DATE_TOKEN_RE.search(norm_txt)

    return {
        "norm_txt": norm_txt,
        "narrative": narrative,
        "tables": tables,
        "kv_pairs": kv_pairs,
        "table_headers": table_headers,
        "source_cols": source_cols,
        "top_text": top_text,
        "full_low": full_low,
        "title_snippet": _clean_title_snippet(norm_txt),
        "date_str": date_m.group(0) if date_m else "",
    }


def _score_page_for_sheet(sig: dict[str, Any], sheet: dict[str, Any]) -> float:
    fields = list(sheet.get("fields") or [])
    non_meta_fields = [f for f in fields if f.get("role") not in ("meta_id", "time_split")]
    eval_fields = non_meta_fields or fields

    # 1. Column / key overlap (0..0.55)
    source_cols = sig["source_cols"]
    col_score = 0.0
    if source_cols and eval_fields:
        matched_pts = 0.0
        for sc in source_cols:
            mf, st = match_column_to_fields(sc, eval_fields)
            if mf is not None:
                matched_pts += 1.0 if st in ("matched_exact", "matched_alias") else 0.65
        denom = max(1, min(len(source_cols), max(len(eval_fields), 2)))
        col_score = min(1.0, matched_pts / float(denom))

    # 2. Keyword hits (0..0.35)
    keywords = [str(k).strip() for k in (sheet.get("match_keywords") or []) if str(k).strip()]
    sheet_name = str(sheet.get("name") or "").strip()
    kw_pts = 0.0
    if sheet_name and sheet_name.lower() in sig["top_text"]:
        kw_pts += 1.4
    elif sheet_name and sheet_name.lower() in sig["full_low"]:
        kw_pts += 0.8
    for kw in keywords:
        kl = kw.lower()
        if kl in sig["top_text"]:
            kw_pts += 1.0
        elif kl in sig["full_low"]:
            kw_pts += 0.55
    kw_score = min(1.0, kw_pts / 1.8) if (keywords or sheet_name) else 0.0

    # 3. Mode affinity (0..0.10)
    mode = str(sheet.get("mode") or "record")
    has_table = any(len(t.get("rows") or []) >= 1 for t in sig["tables"])
    mode_score = 0.0
    if mode == "table":
        mode_score = 1.0 if has_table else (0.25 if len(sig["kv_pairs"]) >= 2 else 0.0)
    elif mode == "record":
        mode_score = 1.0 if (not has_table and len(sig["kv_pairs"]) >= 1) else (0.5 if has_table else 0.2)
    else:  # text / SOP
        mode_score = 1.0 if (not has_table and len(sig["narrative"]) >= 30) else 0.25

    total = 0.55 * col_score + 0.35 * kw_score + 0.10 * mode_score
    return round(total, 4)


def _cast_field_value(raw_val: Any, role: str) -> Any:
    if raw_val is None:
        return None
    if role == "num_feature":
        if isinstance(raw_val, (int, float)) and not isinstance(raw_val, bool):
            return float(raw_val)
        return parse_numeric_value(str(raw_val))
    if role == "target_y":
        if isinstance(raw_val, (int, float)) and not isinstance(raw_val, bool):
            return float(raw_val)
        s = sanitize_excel_text(raw_val).strip()
        if not s:
            return None
        num = parse_numeric_value(s)
        return num if num is not None else s
    s = sanitize_excel_text(raw_val).strip()
    return s or None


def _unique_col_name(base: str, used: set[str], sheet_name: str) -> str:
    clean = re.sub(r"[\s/\\()（）\[\]{}:：]+", "_", base.strip()).strip("_") or "col"
    if clean not in used:
        used.add(clean)
        return clean
    pref = re.sub(r"[\s/\\()（）\[\]{}:：]+", "_", sheet_name.strip()).strip("_")
    cand = f"{pref}_{clean}" if pref else clean
    if cand not in used:
        used.add(cand)
        return cand
    i = 2
    while f"{cand}_{i}" in used:
        i += 1
    out = f"{cand}_{i}"
    used.add(out)
    return out


def _assemble_catboost_dataset(
    sheets_out: list[dict[str, Any]],
    *,
    fallback_batch: str,
) -> dict[str, Any]:
    batches: dict[str, dict[str, Any]] = {}
    col_specs: list[dict[str, Any]] = []
    target_specs: list[dict[str, Any]] = []
    num_stats_lookup: dict[str, tuple[str, str]] = {}
    used_feature_names: set[str] = {"batch_id", "record_date", "avg_ocr_conf", "review_required"}

    def _ensure_batch(bid: str) -> dict[str, Any]:
        if bid not in batches:
            batches[bid] = {
                "batch_id": bid,
                "record_date": "",
                "values": {},
                "num_lists": {},
                "text_lists": {},
                "confs": [],
                "review_required": 0,
            }
        return batches[bid]

    for s in sheets_out:
        s_name = str(s.get("name") or s.get("sheet_id") or "")
        mode = str(s.get("mode") or "record")
        fields = list(s.get("fields") or [])
        rows = list(s.get("rows") or [])
        if not fields:
            continue

        meta_keys = [f["key"] for f in fields if f.get("role") == "meta_id"]
        time_keys = [f["key"] for f in fields if f.get("role") == "time_split"]

        # Map each field in this sheet to output wide-table column specs
        field_out_map: dict[str, dict[str, Any]] = {}
        for f in fields:
            role = str(f.get("role") or "num_feature")
            if role in ("meta_id", "time_split"):
                continue
            fkey = str(f.get("key") or f.get("name"))
            fname = str(f.get("name") or fkey)
            agg = str(f.get("agg") or ("stats" if (mode == "table" and role == "num_feature") else "last"))

            if role == "target_y":
                y_base = fname if fname.lower().startswith("y_") else f"y_{fname}"
                y_col = _unique_col_name(y_base, used_feature_names, s_name)
                spec = {
                    "kind": "target_y",
                    "col_name": y_col,
                    "field_key": fkey,
                    "source_sheet": s_name,
                    "dtype": "float_or_string",
                    "missing_policy": "drop_or_mask_per_task",
                    "description": f"预测目标 y（来自「{s_name}」· {fname}），禁止作为输入 X",
                }
                field_out_map[fkey] = spec
                target_specs.append(spec)
            elif role == "num_feature" and mode == "table" and agg == "stats":
                stat_cols = []
                for stat_key, stat_cn in (("mean", "均值"), ("min", "最小值"), ("max", "最大值"), ("last", "末次值")):
                    cname = _unique_col_name(f"{fname}_{stat_key}", used_feature_names, s_name)
                    stat_cols.append((stat_key, cname, f"多行聚合：{fname} 批次内{stat_cn}（缺失留空 NaN）"))
                spec = {
                    "kind": "num_stats",
                    "stat_cols": stat_cols,
                    "field_key": fkey,
                    "source_sheet": s_name,
                }
                field_out_map[fkey] = spec
                for stat_key, cname, desc in stat_cols:
                    num_stats_lookup[cname] = (f"{s_name}::{fkey}", stat_key)
                    col_specs.append(
                        {
                            "column_name": cname,
                            "catboost_role": "num_feature",
                            "dtype": "float",
                            "source_sheet": s_name,
                            "missing_policy": "native_nan",
                            "description": desc,
                        }
                    )
            elif role == "num_feature":
                cname = _unique_col_name(fname, used_feature_names, s_name)
                spec = {"kind": "num_scalar", "col_name": cname, "field_key": fkey, "source_sheet": s_name}
                field_out_map[fkey] = spec
                col_specs.append(
                    {
                        "column_name": cname,
                        "catboost_role": "num_feature",
                        "dtype": "float",
                        "source_sheet": s_name,
                        "missing_policy": "native_nan",
                        "description": f"数值特征（来自「{s_name}」· 已去单位转浮点，缺失留空 NaN）",
                    }
                )
            elif role == "text_feature":
                cname = _unique_col_name(fname, used_feature_names, s_name)
                spec = {"kind": "text_join", "col_name": cname, "field_key": fkey, "source_sheet": s_name}
                field_out_map[fkey] = spec
                col_specs.append(
                    {
                        "column_name": cname,
                        "catboost_role": "text_feature",
                        "dtype": "string",
                        "source_sheet": s_name,
                        "missing_policy": "empty_token",
                        "description": f"文本特征（来自「{s_name}」· 传给 CatBoost text_features）",
                    }
                )
            else:  # cat_feature
                cname = _unique_col_name(fname, used_feature_names, s_name)
                spec = {"kind": "cat_scalar", "col_name": cname, "field_key": fkey, "source_sheet": s_name}
                field_out_map[fkey] = spec
                col_specs.append(
                    {
                        "column_name": cname,
                        "catboost_role": "cat_feature",
                        "dtype": "string",
                        "source_sheet": s_name,
                        "missing_policy": "empty_token",
                        "description": f"类别特征（来自「{s_name}」· 传给 CatBoost cat_features）",
                    }
                )

        # Accumulate values per batch
        for r in rows:
            meta = r.get("_meta") or {}
            bid = ""
            for mk in meta_keys:
                if r.get(mk):
                    bid = str(r[mk]).strip()
                    break
            if not bid:
                bid = str(meta.get("batch_id") or fallback_batch).strip() or fallback_batch
            b = _ensure_batch(bid)
            conf = float(meta.get("ocr_confidence", 0.0))
            if conf > 0:
                b["confs"].append(conf)
            if meta.get("warnings") or meta.get("cell_alerts"):
                b["review_required"] = 1

            for tk in time_keys:
                raw_t = str(r.get(tk) or "")
                dm = _DATE_TOKEN_RE.search(raw_t)
                if dm and not b["record_date"]:
                    b["record_date"] = dm.group(0)
            if not b["record_date"] and meta.get("date_str"):
                b["record_date"] = str(meta["date_str"])

            for fkey, spec in field_out_map.items():
                val = r.get(fkey)
                if val is None or val == "":
                    continue
                kind = spec["kind"]
                if kind == "num_stats":
                    num_v = val if isinstance(val, (int, float)) else parse_numeric_value(str(val))
                    if num_v is not None:
                        b["num_lists"].setdefault(f"{s_name}::{fkey}", []).append(float(num_v))
                elif kind == "num_scalar":
                    num_v = val if isinstance(val, (int, float)) else parse_numeric_value(str(val))
                    if num_v is not None:
                        b["values"][spec["col_name"]] = round(float(num_v), 4)
                elif kind == "text_join":
                    txt_v = sanitize_excel_text(val).strip()
                    if txt_v:
                        b["text_lists"].setdefault(spec["col_name"], []).append(txt_v)
                elif kind == "target_y":
                    b["values"][spec["col_name"]] = val
                else:  # cat_scalar
                    b["values"][spec["col_name"]] = sanitize_excel_text(val).strip()

    # Final column & schema order: meta -> time -> features -> qa_gate -> target_y
    ordered_specs: list[dict[str, Any]] = [
        {
            "column_name": "batch_id",
            "catboost_role": "meta_id",
            "dtype": "string",
            "source_sheet": "全局主键",
            "missing_policy": "drop_row",
            "description": "主建模粒度主键：1 批次 = 1 行；切分训练/验证集按时间+批次整批隔离，禁止同批拆散",
        },
        {
            "column_name": "record_date",
            "catboost_role": "time_split",
            "dtype": "date",
            "source_sheet": "全局时间",
            "missing_policy": "forward_fill",
            "description": "批次规范化日期 (YYYY-MM-DD)，用于严格时间序切分验证集",
        },
        *col_specs,
        {
            "column_name": "avg_ocr_conf",
            "catboost_role": "num_feature",
            "dtype": "float",
            "source_sheet": "质检系统",
            "missing_policy": "native_nan",
            "description": "批次关联页面平均 OCR 识别置信度（0~1）",
        },
        {
            "column_name": "review_required",
            "catboost_role": "qa_gate",
            "dtype": "int",
            "source_sheet": "质检系统",
            "missing_policy": "drop_row",
            "description": "质量隔离门：0=可直接入训；1=含低置信或原页告警，禁止回流训练集",
        },
    ]
    for ts in target_specs:
        ordered_specs.append(
            {
                "column_name": ts["col_name"],
                "catboost_role": "target_y",
                "dtype": ts["dtype"],
                "source_sheet": ts["source_sheet"],
                "missing_policy": ts["missing_policy"],
                "description": ts["description"],
            }
        )

    headers = [c["column_name"] for c in ordered_specs]
    target_y_cols = [ts["col_name"] for ts in target_specs]

    # Build final 1-row-per-batch records
    records: list[dict[str, Any]] = []

    for bid, b in batches.items():
        rec: dict[str, Any] = {}
        for spec in ordered_specs:
            cname = spec["column_name"]
            role = spec["catboost_role"]
            if cname == "batch_id":
                rec[cname] = bid
            elif cname == "record_date":
                rec[cname] = b["record_date"] or None
            elif cname == "avg_ocr_conf":
                rec[cname] = round(sum(b["confs"]) / len(b["confs"]), 4) if b["confs"] else None
            elif cname == "review_required":
                rec[cname] = int(b["review_required"])
            elif cname in num_stats_lookup:
                series_key, agg_k = num_stats_lookup[cname]
                vals = b["num_lists"].get(series_key) or []
                if not vals:
                    rec[cname] = None
                elif agg_k == "mean":
                    rec[cname] = round(sum(vals) / len(vals), 4)
                elif agg_k == "min":
                    rec[cname] = round(min(vals), 4)
                elif agg_k == "max":
                    rec[cname] = round(max(vals), 4)
                else:
                    rec[cname] = round(vals[-1], 4)
            elif role == "text_feature":
                items = list(dict.fromkeys(b["text_lists"].get(cname) or []))
                rec[cname] = sanitize_excel_text(" | ".join(items))[:2000] if items else None
            else:
                rec[cname] = b["values"].get(cname, None)
        records.append(rec)

    return {
        "headers": headers,
        "target_y_cols": target_y_cols,
        "rows": records,
        "schema_rows": ordered_specs,
    }


def _parse_optional_bound(val: Any) -> float | None:
    if val is None or isinstance(val, bool) or val == "":
        return None
    try:
        num = float(val)
        return num if math.isfinite(num) else None
    except (TypeError, ValueError):
        return None


def _format_range_text(min_v: float | None, max_v: float | None) -> str:
    if min_v is not None and max_v is not None:
        return f"{min_v:g}~{max_v:g}"
    if min_v is not None:
        return f">={min_v:g}"
    if max_v is not None:
        return f"<={max_v:g}"
    return ""


def _resolve_row_batch_id(
    row: dict[str, Any],
    meta_keys: list[str],
    fallback_batch: str,
) -> str:
    for mk in meta_keys:
        val = str(row.get(mk) or "").strip()
        if val:
            return val
    meta = row.get("_meta") or {}
    return str(meta.get("batch_id") or fallback_batch).strip() or fallback_batch


def _add_row_cell_alert(row: dict[str, Any], field_key: str, msg: str) -> bool:
    meta = row.setdefault("_meta", {})
    alerts = meta.setdefault("cell_alerts", {})
    warns = meta.setdefault("warnings", [])
    meta["has_verifiable_error"] = True

    existing = alerts.get(field_key, "")
    if msg not in existing:
        alerts[field_key] = f"{existing}；{msg}" if existing else msg
    if msg not in warns:
        warns.append(msg)
        return True
    return False


def _audit_sheet_verifiable_checks(
    sheet_buckets: dict[str, dict[str, Any]],
    warning_rows: list[dict[str, Any]],
    fallback_batch: str,
) -> int:
    """Run deterministic field range bounds, not-null checks, and intra-batch >5x outlier audits."""
    verifiable_alert_count = 0

    for sid, bucket in sheet_buckets.items():
        sheet_name = str(bucket.get("name") or sid)
        fields = list(bucket.get("fields") or [])
        rows = list(bucket.get("rows") or [])
        meta_keys = [str(f.get("key") or "") for f in fields if f.get("role") == "meta_id" and f.get("key")]
        row_new_alerts: dict[int, list[str]] = {}

        # 1. Per-row required & min/max physical bounds
        for r_idx, row in enumerate(rows):
            meta = row.setdefault("_meta", {})
            meta.setdefault("cell_alerts", {})
            meta.setdefault("warnings", [])

            for f in fields:
                fk = str(f.get("key") or "")
                fname = str(f.get("name") or fk)
                if not fk:
                    continue
                raw_val = row.get(fk)
                is_empty = raw_val is None or (isinstance(raw_val, str) and not raw_val.strip())

                if bool(f.get("require_not_null", False)) and is_empty:
                    msg = f"必填缺失: {fname}不能为空"
                    if _add_row_cell_alert(row, fk, msg):
                        row_new_alerts.setdefault(r_idx, []).append(msg)
                        verifiable_alert_count += 1
                    continue

                if is_empty:
                    continue

                min_v = _parse_optional_bound(f.get("min_val"))
                max_v = _parse_optional_bound(f.get("max_val"))
                if min_v is None and max_v is None:
                    continue

                num_v = (
                    float(raw_val)
                    if isinstance(raw_val, (int, float)) and not isinstance(raw_val, bool)
                    else parse_numeric_value(str(raw_val))
                )
                if num_v is not None and math.isfinite(num_v):
                    if (min_v is not None and num_v < min_v) or (max_v is not None and num_v > max_v):
                        rng_str = _format_range_text(min_v, max_v)
                        msg = f"越界异常: {fname}={num_v:g} (合理范围 {rng_str})"
                        if _add_row_cell_alert(row, fk, msg):
                            row_new_alerts.setdefault(r_idx, []).append(msg)
                            verifiable_alert_count += 1

        # 2. Intra-batch >5x decimal-shift outlier check across numeric metrics
        for f in fields:
            role = str(f.get("role") or "")
            if role not in ("num_feature", "target_y"):
                continue
            fk = str(f.get("key") or "")
            fname = str(f.get("name") or fk)
            if not fk:
                continue
            min_v = _parse_optional_bound(f.get("min_val"))
            max_v = _parse_optional_bound(f.get("max_val"))

            by_batch: dict[str, list[tuple[int, dict[str, Any], float]]] = {}
            for r_idx, row in enumerate(rows):
                val = row.get(fk)
                num_v = (
                    float(val)
                    if isinstance(val, (int, float)) and not isinstance(val, bool)
                    else (parse_numeric_value(str(val)) if val is not None else None)
                )
                if num_v is not None and math.isfinite(num_v) and num_v > 0:
                    bid = _resolve_row_batch_id(row, meta_keys, fallback_batch)
                    by_batch.setdefault(bid, []).append((r_idx, row, float(num_v)))

            for bid, pairs in by_batch.items():
                if len(pairs) < 2:
                    continue
                if len(pairs) >= 3:
                    med = float(statistics.median(v for _, _, v in pairs))
                    if med <= 0:
                        continue
                    for r_idx, row, v in pairs:
                        if (v / med) > 5.0 or (med / v) > 5.0:
                            msg = f"离群突变(>5倍): {fname}={v:g} (同批中位数 {med:g}，疑似小数点错位)"
                            if _add_row_cell_alert(row, fk, msg):
                                row_new_alerts.setdefault(r_idx, []).append(msg)
                                verifiable_alert_count += 1
                else:
                    (idx_lo, row_lo, v_lo), (idx_hi, row_hi, v_hi) = sorted(pairs, key=lambda x: x[2])
                    if v_lo > 0 and (v_hi / v_lo) > 5.0:
                        lo_oob = (min_v is not None and v_lo < min_v) or (max_v is not None and v_lo > max_v)
                        hi_oob = (min_v is not None and v_hi < min_v) or (max_v is not None and v_hi > max_v)
                        if lo_oob and not hi_oob:
                            bad_idx, bad_row, bad_v, ref_v = idx_lo, row_lo, v_lo, v_hi
                        else:
                            bad_idx, bad_row, bad_v, ref_v = idx_hi, row_hi, v_hi, v_lo
                        msg = f"离群突变(>5倍): {fname}={bad_v:g} (同批对照 {ref_v:g}，疑似小数点错位)"
                        if _add_row_cell_alert(bad_row, fk, msg):
                            row_new_alerts.setdefault(bad_idx, []).append(msg)
                            verifiable_alert_count += 1

        for r_idx, alerts in sorted(row_new_alerts.items()):
            row = rows[r_idx]
            meta = row.get("_meta") or {}
            bid = _resolve_row_batch_id(row, meta_keys, fallback_batch)
            warning_rows.append(
                {
                    "sheet_name": sheet_name,
                    "page_no": int(meta.get("page_no", 0)),
                    "warnings": list(alerts),
                    "summary": f"[{bid}] " + "；".join(alerts),
                    "ocr_confidence": float(meta.get("ocr_confidence", 0.0)),
                    "severity": "error",
                }
            )

    return verifiable_alert_count


def _apply_redactor_to_pack(
    sheet_buckets: dict[str, dict[str, Any]],
    unassigned_pages: list[dict[str, Any]],
    ignored_pages: list[dict[str, Any]],
    warning_rows: list[dict[str, Any]],
    redactor: "ConsistentPIIRedactor",
) -> None:
    """Apply consistent PII masking in-place prior to CatBoost wide-table assembly."""
    if not redactor or not redactor.enabled:
        return

    for sb in sheet_buckets.values():
        field_name_by_key = {
            str(f.get("key") or ""): str(f.get("name") or f.get("key") or "")
            for f in (sb.get("fields") or [])
            if f.get("key")
        }
        for row in sb.get("rows") or []:
            for fk, fname in field_name_by_key.items():
                if fk in row:
                    row[fk] = redactor.redact_cell(row[fk], field_name=fname)
            meta = row.get("_meta") or {}
            if meta.get("batch_id"):
                meta["batch_id"] = redactor.redact_text(str(meta["batch_id"]))
            if meta.get("warnings"):
                meta["warnings"] = [redactor.redact_text(str(w)) for w in meta["warnings"]]
            if meta.get("cell_alerts"):
                meta["cell_alerts"] = {
                    k: redactor.redact_text(str(v))
                    for k, v in (meta["cell_alerts"] or {}).items()
                }
        for page_card in sb.get("assigned_pages") or []:
            if page_card.get("title"):
                page_card["title"] = redactor.redact_text(str(page_card["title"]))
            if page_card.get("batch_id"):
                page_card["batch_id"] = redactor.redact_text(str(page_card["batch_id"]))

    for list_pages in (unassigned_pages, ignored_pages):
        for page_card in list_pages:
            if page_card.get("title"):
                page_card["title"] = redactor.redact_text(str(page_card["title"]))
            if page_card.get("batch_id"):
                page_card["batch_id"] = redactor.redact_text(str(page_card["batch_id"]))

    for w in warning_rows:
        if w.get("summary"):
            w["summary"] = redactor.redact_text(str(w["summary"]))
        if w.get("warnings"):
            w["warnings"] = [redactor.redact_text(str(item)) for item in w["warnings"]]


def match_book_to_template_pack(
    book: dict[str, Any],
    pages: list[dict[str, Any]],
    template: dict[str, Any],
    assignments: dict[int, dict[str, Any]] | None = None,
    *,
    redactor: "ConsistentPIIRedactor | None" = None,
) -> dict[str, Any]:
    """Route scanned pages into multi-sheet template drawers, match columns, and assemble CatBoost table."""
    rules = CleaningRules.from_dict(template.get("rules"))
    raw_sheets = list(template.get("sheets") or [])
    assignments = assignments or {}
    running_headers = detect_running_headers(pages, rules)

    fallback_batch = sanitize_excel_text(book.get("isbn") or book.get("title") or "BATCH_001")
    fallback_batch = re.sub(r"\s+", "_", fallback_batch).strip("_")[:32] or "BATCH_001"

    sheet_buckets: dict[str, dict[str, Any]] = {}
    for s in raw_sheets:
        sid = str(s.get("sheet_id") or "")
        if not sid:
            continue
        sheet_buckets[sid] = {
            **s,
            "fields": [dict(f) for f in (s.get("fields") or [])],
            "assigned_pages": [],
            "rows": [],
            "_source_col_samples": {},  # src_col -> {"samples": [], "count": int}
        }

    unassigned_pages: list[dict[str, Any]] = []
    ignored_pages: list[dict[str, Any]] = []
    warning_rows: list[dict[str, Any]] = []
    current_batch_id = ""
    matched_pages_count = 0

    for page in pages:
        page_id = int(page.get("id", 0))
        page_no = int(page.get("seq_no", 0))
        conf = float(page.get("ocr_confidence", 0.0))
        qa_flags = list(page.get("qa_flags") or [])
        sig = _extract_page_signals(page, rules, running_headers)
        current_batch_id = detect_batch_id(sig["norm_txt"], current_batch_id, fallback_batch)

        page_warnings: list[str] = []
        if 0.0 < conf < rules.min_confidence_warn:
            page_warnings.append(f"OCR低置信度({conf * 100:.1f}%)")
        elif conf == 0.0 and not (page.get("clean_text") or page.get("ocr_text")):
            page_warnings.append("无识别文本")
        if qa_flags:
            page_warnings.append(f"原页质检告警:{','.join(qa_flags)}")

        assign = assignments.get(page_id)
        if assign and assign.get("sheet_id") == "__ignore__":
            ignored_pages.append(
                {
                    "page_id": page_id,
                    "seq_no": page_no,
                    "title": sig["title_snippet"],
                    "thumb_url": f"/api/pages/{page_id}/image/thumb",
                    "status": str(page.get("status") or ""),
                    "ocr_confidence": round(conf, 4),
                    "qa_flags": qa_flags,
                    "match_source": "ignored",
                }
            )
            continue

        chosen_sid = ""
        match_source = "auto"
        best_score = 0.0
        suggested_sid = ""
        suggested_name = ""

        # Compute scores across all sheets (also gives helpful suggestion for unassigned pages)
        scored: list[tuple[float, str]] = []
        for sid, sb in sheet_buckets.items():
            sc = _score_page_for_sheet(sig, sb)
            scored.append((sc, sid))
        scored.sort(reverse=True, key=lambda x: x[0])
        if scored:
            best_score, suggested_sid = scored[0]
            suggested_name = str(sheet_buckets[suggested_sid].get("name") or "")

        if assign and assign.get("sheet_id") in sheet_buckets:
            chosen_sid = str(assign["sheet_id"])
            match_source = "llm" if assign.get("llm_rows") else "manual"
            best_score = 1.0
        elif scored and best_score >= 0.22:
            chosen_sid = suggested_sid
            match_source = "llm" if (assign and assign.get("llm_rows")) else "auto"

        page_card = {
            "page_id": page_id,
            "seq_no": page_no,
            "title": sig["title_snippet"],
            "batch_id": current_batch_id,
            "date_str": sig["date_str"],
            "thumb_url": f"/api/pages/{page_id}/image/thumb",
            "status": str(page.get("status") or ""),
            "ocr_confidence": round(conf, 4),
            "qa_flags": qa_flags,
            "warnings": page_warnings,
            "match_source": match_source,
            "match_score": best_score,
            "source_cols": sig["source_cols"],
            "suggested_sheet_id": suggested_sid,
            "suggested_sheet_name": suggested_name,
        }

        if not chosen_sid:
            unassigned_pages.append(page_card)
            warning_rows.append(
                {
                    "sheet_name": "待确认归类",
                    "page_no": page_no,
                    "warnings": ["未归类到任何模板子表", *page_warnings],
                    "summary": sig["title_snippet"],
                    "ocr_confidence": conf,
                }
            )
            continue

        matched_pages_count += 1
        bucket = sheet_buckets[chosen_sid]
        bucket["assigned_pages"].append(page_card)

        fields = bucket["fields"]
        mode = str(bucket.get("mode") or "record")

        # Track discovered source columns & sample values for Column Mapping UI
        for src_col in sig["source_cols"]:
            entry = bucket["_source_col_samples"].setdefault(src_col, {"samples": [], "count": 0})
            entry["count"] += 1
            sample_val = sig["kv_pairs"].get(src_col)
            if sample_val is None and sig["tables"]:
                for tbl in sig["tables"]:
                    hdrs = [str(h).strip() for h in (tbl.get("headers") or [])]
                    if src_col in hdrs:
                        idx_c = hdrs.index(src_col)
                        for r in tbl.get("rows") or []:
                            if idx_c < len(r) and str(r[idx_c]).strip():
                                sample_val = str(r[idx_c]).strip()
                                break
                        if sample_val:
                            break
            if sample_val and sample_val not in entry["samples"] and len(entry["samples"]) < 2:
                entry["samples"].append(str(sample_val)[:48])

        # Extract rows for this page into the chosen Sheet
        extracted_page_rows: list[dict[str, Any]] = []
        if assign and assign.get("llm_rows"):
            for raw_lr in assign["llm_rows"]:
                row_obj: dict[str, Any] = {}
                for f in fields:
                    fk = str(f.get("key") or "")
                    role = str(f.get("role") or "num_feature")
                    row_obj[fk] = _cast_field_value(raw_lr.get(fk), role)
                # Ensure meta_id fallback
                for f in fields:
                    if f.get("role") == "meta_id" and not row_obj.get(f["key"]):
                        row_obj[f["key"]] = current_batch_id
                row_obj["_meta"] = {
                    "page_id": page_id,
                    "page_no": page_no,
                    "batch_id": current_batch_id,
                    "date_str": sig["date_str"],
                    "warnings": list(page_warnings),
                    "ocr_confidence": conf,
                    "source": "llm",
                }
                extracted_page_rows.append(row_obj)
        elif mode == "table" and sig["tables"]:
            for tbl in sig["tables"]:
                hdrs = [sanitize_excel_text(h).strip() for h in (tbl.get("headers") or [])]
                col_field_pairs = [match_column_to_fields(h, fields)[0] for h in hdrs]
                for t_row in tbl.get("rows") or []:
                    row_obj = {str(f["key"]): None for f in fields if f.get("key")}
                    # Seed with page-level KV pairs for context columns (e.g. batch_id, date)
                    for kv_k, kv_v in sig["kv_pairs"].items():
                        mf, _ = match_column_to_fields(kv_k, fields)
                        if mf is not None and row_obj.get(mf["key"]) is None:
                            row_obj[mf["key"]] = _cast_field_value(kv_v, str(mf.get("role") or ""))
                    for idx_c, cell_v in enumerate(t_row):
                        mf = col_field_pairs[idx_c] if idx_c < len(col_field_pairs) else None
                        if mf is not None:
                            row_obj[mf["key"]] = _cast_field_value(cell_v, str(mf.get("role") or ""))
                    for f in fields:
                        if f.get("role") == "meta_id" and not row_obj.get(f["key"]):
                            row_obj[f["key"]] = current_batch_id
                        elif f.get("role") == "time_split" and not row_obj.get(f["key"]) and sig["date_str"]:
                            row_obj[f["key"]] = sig["date_str"]
                    row_obj["_meta"] = {
                        "page_id": page_id,
                        "page_no": page_no,
                        "batch_id": current_batch_id,
                        "date_str": sig["date_str"],
                        "warnings": list(page_warnings),
                        "ocr_confidence": conf,
                        "source": "offline",
                    }
                    extracted_page_rows.append(row_obj)
        elif mode == "text":
            row_obj = {str(f["key"]): None for f in fields if f.get("key")}
            for kv_k, kv_v in sig["kv_pairs"].items():
                mf, _ = match_column_to_fields(kv_k, fields)
                if mf is not None and row_obj.get(mf["key"]) is None:
                    row_obj[mf["key"]] = _cast_field_value(kv_v, str(mf.get("role") or ""))
            cat_filled = False
            txt_filled = False
            for f in fields:
                role = str(f.get("role") or "")
                fk = str(f.get("key") or "")
                if role == "meta_id" and not row_obj.get(fk):
                    row_obj[fk] = current_batch_id
                elif role == "time_split" and not row_obj.get(fk) and sig["date_str"]:
                    row_obj[fk] = sig["date_str"]
                elif role == "cat_feature" and not cat_filled and not row_obj.get(fk):
                    row_obj[fk] = sig["title_snippet"]
                    cat_filled = True
                elif role == "text_feature" and not txt_filled and not row_obj.get(fk):
                    row_obj[fk] = sanitize_excel_text(sig["narrative"] or sig["norm_txt"]).strip()
                    txt_filled = True
            row_obj["_meta"] = {
                "page_id": page_id,
                "page_no": page_no,
                "batch_id": current_batch_id,
                "date_str": sig["date_str"],
                "warnings": list(page_warnings),
                "ocr_confidence": conf,
                "source": "offline",
            }
            extracted_page_rows.append(row_obj)
        else:  # record mode (or table sheet fallback on non-table page)
            row_obj = {str(f["key"]): None for f in fields if f.get("key")}
            # Also map single-row horizontal tables if present
            for tbl in sig["tables"]:
                hdrs = [sanitize_excel_text(h).strip() for h in (tbl.get("headers") or [])]
                trows = tbl.get("rows") or []
                if trows:
                    last_r = trows[-1]
                    for idx_c, h in enumerate(hdrs):
                        if idx_c < len(last_r):
                            mf, _ = match_column_to_fields(h, fields)
                            if mf is not None and row_obj.get(mf["key"]) is None:
                                row_obj[mf["key"]] = _cast_field_value(last_r[idx_c], str(mf.get("role") or ""))
            for kv_k, kv_v in sig["kv_pairs"].items():
                mf, _ = match_column_to_fields(kv_k, fields)
                if mf is not None and row_obj.get(mf["key"]) is None:
                    row_obj[mf["key"]] = _cast_field_value(kv_v, str(mf.get("role") or ""))
            for f in fields:
                role = str(f.get("role") or "")
                fk = str(f.get("key") or "")
                if role == "meta_id" and not row_obj.get(fk):
                    row_obj[fk] = current_batch_id
                elif role == "time_split" and not row_obj.get(fk) and sig["date_str"]:
                    row_obj[fk] = sig["date_str"]
                elif role == "text_feature" and not row_obj.get(fk):
                    row_obj[fk] = sanitize_excel_text(sig["narrative"] or sig["norm_txt"]).strip()[:1000]
            row_obj["_meta"] = {
                "page_id": page_id,
                "page_no": page_no,
                "batch_id": current_batch_id,
                "date_str": sig["date_str"],
                "warnings": list(page_warnings),
                "ocr_confidence": conf,
                "source": "offline",
            }
            extracted_page_rows.append(row_obj)

        bucket["rows"].extend(extracted_page_rows)
        if page_warnings:
            warning_rows.append(
                {
                    "sheet_name": str(bucket.get("name") or chosen_sid),
                    "page_no": page_no,
                    "warnings": page_warnings,
                    "summary": sig["title_snippet"],
                    "ocr_confidence": conf,
                }
            )

    verifiable_alert_count = _audit_sheet_verifiable_checks(
        sheet_buckets,
        warning_rows,
        fallback_batch=fallback_batch,
    )

    if redactor is not None and redactor.enabled:
        _apply_redactor_to_pack(
            sheet_buckets,
            unassigned_pages,
            ignored_pages,
            warning_rows,
            redactor,
        )

    # Finalize column_mappings per sheet + overall coverage
    sheets_out: list[dict[str, Any]] = []
    total_src_cols = 0
    aligned_src_cols = 0
    for sid, sb in sheet_buckets.items():
        fields = sb["fields"]
        mappings: list[dict[str, Any]] = []
        for src_col, info in sb["_source_col_samples"].items():
            mf, status = match_column_to_fields(src_col, fields)
            total_src_cols += 1
            if mf is not None:
                aligned_src_cols += 1
            samples = info["samples"]
            if redactor is not None and redactor.enabled:
                samples = [redactor.redact_text(str(sv)) for sv in samples]
            mappings.append(
                {
                    "source_col": src_col,
                    "sample_values": samples,
                    "count": info["count"],
                    "target_field_key": mf["key"] if mf else None,
                    "target_field_name": mf["name"] if mf else None,
                    "target_role": mf.get("role") if mf else None,
                    "status": status,
                }
            )
        sb.pop("_source_col_samples", None)
        sb["column_mappings"] = mappings
        sheets_out.append(sb)

    catboost_dataset = _assemble_catboost_dataset(sheets_out, fallback_batch=fallback_batch)
    total_pages = len(pages)
    alignment_pct = round(100.0 * aligned_src_cols / total_src_cols, 1) if total_src_cols > 0 else 100.0

    return {
        "template_id": template.get("id"),
        "template_name": template.get("name", ""),
        "coverage": {
            "total_pages": total_pages,
            "matched_pages": matched_pages_count,
            "unassigned_pages": len(unassigned_pages),
            "ignored_pages": len(ignored_pages),
            "total_source_cols": total_src_cols,
            "aligned_source_cols": aligned_src_cols,
            "alignment_pct": alignment_pct,
            "sheet_count": len(sheets_out),
            "target_y_names": catboost_dataset["target_y_cols"],
            "total_batches": len(catboost_dataset["rows"]),
            "verifiable_alert_count": verifiable_alert_count,
        },
        "sheets": sheets_out,
        "unassigned_pages": unassigned_pages,
        "ignored_pages": ignored_pages,
        "warning_rows": warning_rows,
        "catboost_dataset": catboost_dataset,
    }
