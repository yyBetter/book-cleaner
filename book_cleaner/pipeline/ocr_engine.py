"""L2 Offline OCR Engine (RapidOCR ONNX Runtime) and layout paragraph formatter."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np


_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
_PAGE_NUM_RE = re.compile(r"^[-—·~\[\]（）()\s]*\d{1,5}[-—·~\[\]（）()\s]*$|^第?\s*\d{1,4}\s*页$")
_CHAPTER_HEADING_RE = re.compile(
    r"^(第[一二三四五六七八九十百千万零两0-9]+[章节回篇卷部节].{0,32}|Chapter\s+\d+.*)$",
    re.IGNORECASE,
)
_LIST_ITEM_RE = re.compile(
    r"^(?:\d{1,3}[.、)）]|[一二三四五六七八九十]+[、.）)]|[-*•·])\s*\S"
)


@dataclass(slots=True)
class OCRBlock:
    bbox: list[list[float]]
    text: str
    confidence: float

    @property
    def x_min(self) -> float:
        return min(pt[0] for pt in self.bbox)

    @property
    def x_max(self) -> float:
        return max(pt[0] for pt in self.bbox)

    @property
    def y_min(self) -> float:
        return min(pt[1] for pt in self.bbox)

    @property
    def y_max(self) -> float:
        return max(pt[1] for pt in self.bbox)

    @property
    def y_center(self) -> float:
        return 0.5 * (self.y_min + self.y_max)

    @property
    def height(self) -> float:
        return max(1.0, self.y_max - self.y_min)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bbox": [[round(float(x), 1), round(float(y), 1)] for x, y in self.bbox],
            "text": self.text,
            "confidence": round(float(self.confidence), 4),
        }


@dataclass(slots=True)
class OCRResult:
    blocks: list[OCRBlock] = field(default_factory=list)
    raw_text: str = ""
    formatted_text: str = ""
    avg_confidence: float = 0.0
    min_confidence: float = 0.0
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocks": [b.to_dict() for b in self.blocks],
            "raw_text": self.raw_text,
            "formatted_text": self.formatted_text,
            "avg_confidence": round(self.avg_confidence, 4),
            "min_confidence": round(self.min_confidence, 4),
            "flags": list(self.flags),
        }


def _smart_join(left: str, right: str) -> str:
    left_s = left.rstrip()
    right_s = right.lstrip()
    if not left_s:
        return right_s
    if not right_s:
        return left_s
    # De-hyphenate ASCII line wraps like "process-\ning"
    if len(left_s) >= 2 and left_s[-1] == "-" and left_s[-2].isascii() and left_s[-2].isalpha():
        if right_s[0].isascii() and right_s[0].isalpha():
            return left_s[:-1] + right_s
    # No space between CJK glyphs
    if _CJK_RE.match(left_s[-1]) or _CJK_RE.match(right_s[0]):
        return left_s + right_s
    return f"{left_s} {right_s}"


def _escape_md_cell(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).replace("|", r"\|")


def _build_markdown_table(run_lines: list[dict[str, Any]], med_h: float) -> str | None:
    """Reconstruct a GFM Markdown pipe table from contiguous multi-column OCR lines."""
    if len(run_lines) < 2:
        return None
    all_x0 = sorted(c["x_min"] for lr in run_lines for c in lr["cells"])
    if not all_x0:
        return None

    tol = max(24.0, med_h * 2.0)
    buckets: list[list[float]] = []
    for x in all_x0:
        if not buckets or abs(x - (sum(buckets[-1]) / len(buckets[-1]))) > tol:
            buckets.append([x])
        else:
            buckets[-1].append(x)
    col_anchors = [sum(b) / len(b) for b in buckets]
    if not (2 <= len(col_anchors) <= 14):
        return None

    grid: list[list[str]] = []
    for lr in run_lines:
        slots = [""] * len(col_anchors)
        for cell in lr["cells"]:
            c_idx = min(range(len(col_anchors)), key=lambda i: abs(cell["x_min"] - col_anchors[i]))
            val = _escape_md_cell(cell["text"])
            if val:
                slots[c_idx] = f"{slots[c_idx]} {val}".strip() if slots[c_idx] else val
        if sum(1 for s in slots if s) >= 2:
            grid.append(slots)

    if len(grid) < 2:
        return None
    populated_cols = sum(
        1 for c_idx in range(len(col_anchors)) if sum(1 for r in grid if r[c_idx]) >= 2
    )
    if populated_cols < 2:
        return None

    md_lines = [
        "| " + " | ".join(grid[0]) + " |",
        "| " + " | ".join("---" for _ in col_anchors) + " |",
    ]
    for row_vals in grid[1:]:
        md_lines.append("| " + " | ".join(row_vals) + " |")
    return "\n".join(md_lines)


def format_ocr_blocks(blocks: list[OCRBlock], img_height: int, img_width: int) -> str:
    """Group OCR boxes into reading order, reconstruct Markdown headings/lists/tables, and join prose."""
    if not blocks:
        return ""

    h = max(1, int(img_height))
    w = max(1, int(img_width))

    filtered: list[OCRBlock] = []
    for b in blocks:
        txt = b.text.strip()
        if not txt:
            continue
        in_top_margin = b.y_center < 0.065 * h
        in_bottom_margin = b.y_center > 0.935 * h
        if (in_top_margin or in_bottom_margin) and len(txt) <= 10 and _PAGE_NUM_RE.match(txt):
            continue
        filtered.append(b)

    if not filtered:
        return ""

    sorted_blocks = sorted(filtered, key=lambda b: (b.y_center, b.x_min))

    # Group into visual lines
    lines: list[list[OCRBlock]] = []
    for blk in sorted_blocks:
        placed = False
        for row in reversed(lines[-4:]):
            row_yc = float(np.mean([r.y_center for r in row]))
            row_h = float(np.median([r.height for r in row]))
            if abs(blk.y_center - row_yc) <= 0.55 * min(blk.height, row_h):
                row.append(blk)
                placed = True
                break
        if not placed:
            lines.append([blk])

    global_med_h = float(np.median([b.height for b in filtered]))
    line_records: list[dict[str, Any]] = []
    for row in lines:
        row.sort(key=lambda b: b.x_min)
        cells: list[dict[str, Any]] = []
        for idx, b in enumerate(row):
            b_txt = b.text.strip()
            if not b_txt:
                continue
            if not cells:
                cells.append({"text": b_txt, "x_min": b.x_min, "x_max": b.x_max})
            else:
                gap = b.x_min - cells[-1]["x_max"]
                if gap > max(global_med_h, b.height) * 1.15:
                    cells.append({"text": b_txt, "x_min": b.x_min, "x_max": b.x_max})
                else:
                    cells[-1]["text"] = _smart_join(cells[-1]["text"], b_txt)
                    cells[-1]["x_max"] = max(cells[-1]["x_max"], b.x_max)
        if not cells:
            continue
        merged = "  ".join(c["text"] for c in cells).strip()
        if not merged:
            continue
        line_records.append(
            {
                "text": merged,
                "cells": cells,
                "x_min": min(b.x_min for b in row),
                "x_max": max(b.x_max for b in row),
                "y_min": min(b.y_min for b in row),
                "y_max": max(b.y_max for b in row),
                "height": float(np.median([b.height for b in row])),
            }
        )

    if not line_records:
        return ""

    med_h = float(np.median([lr["height"] for lr in line_records]))
    left_margin = float(np.percentile([lr["x_min"] for lr in line_records], 20))
    right_margin = float(np.percentile([lr["x_max"] for lr in line_records], 85))
    text_span = max(1.0, right_margin - left_margin)

    for lr in line_records:
        txt = lr["text"]
        lr["is_heading"] = (
            len(lr["cells"]) == 1
            and (
                (lr["height"] >= med_h * 1.26 and len(txt) <= 42 and txt[-1] not in "。！？，,.;；")
                or (_CHAPTER_HEADING_RE.match(txt) is not None and len(txt) <= 46)
            )
        )

    blocks_out: list[str] = []
    current_para = ""
    prev_lr: dict[str, Any] | None = None

    def _flush_para() -> None:
        nonlocal current_para, prev_lr
        if current_para:
            blocks_out.append(current_para)
            current_para = ""
        prev_lr = None

    def _emit_prose_line(lr: dict[str, Any]) -> None:
        nonlocal current_para, prev_lr
        txt = lr["text"]
        if lr["is_heading"]:
            _flush_para()
            blocks_out.append(f"## {txt}")
            return

        if _LIST_ITEM_RE.match(txt):
            _flush_para()
            current_para = txt
            prev_lr = lr
            return

        if prev_lr is None or not current_para:
            current_para = txt
            prev_lr = lr
            return

        vert_gap = lr["y_min"] - prev_lr["y_max"]
        indented = lr["x_min"] > left_margin + max(med_h * 1.1, 0.03 * w)
        prev_short_terminal = (
            prev_lr["x_max"] < right_margin - 0.14 * text_span
            and prev_lr["text"][-1] in "。！？；：.!?:”\"』」"
        )
        new_para = (vert_gap > 0.85 * med_h) or indented or prev_short_terminal

        if new_para:
            blocks_out.append(current_para)
            current_para = txt
        else:
            current_para = _smart_join(current_para, txt)
        prev_lr = lr

    idx = 0
    while idx < len(line_records):
        lr = line_records[idx]
        if not lr["is_heading"] and len(lr["cells"]) >= 2:
            run = [lr]
            j = idx + 1
            while j < len(line_records):
                nxt = line_records[j]
                if nxt["is_heading"] or len(nxt["cells"]) < 2:
                    break
                if nxt["y_min"] - run[-1]["y_max"] > med_h * 2.8:
                    break
                run.append(nxt)
                j += 1
            md_table = _build_markdown_table(run, med_h)
            if md_table is not None:
                _flush_para()
                blocks_out.append(md_table)
                idx = j
                continue

        _emit_prose_line(lr)
        idx += 1

    _flush_para()
    return "\n\n".join(blocks_out)


def reformat_page_from_blocks(page: dict[str, Any]) -> str | None:
    """Recompute standard Markdown formatted text from stored page ocr_blocks."""
    raw_blocks = page.get("ocr_blocks") or []
    blocks: list[OCRBlock] = []
    for item in raw_blocks:
        bbox = item.get("bbox")
        txt = str(item.get("text") or "").strip()
        if not bbox or len(bbox) < 4 or not txt:
            continue
        blocks.append(
            OCRBlock(
                bbox=[[float(pt[0]), float(pt[1])] for pt in bbox],
                text=txt,
                confidence=float(item.get("confidence", 0.0)),
            )
        )
    if not blocks:
        return None
    meta = page.get("clean_meta") or {}
    img_h = int(meta.get("output_height") or 0) or int(max(b.y_max for b in blocks) * 1.1)
    img_w = int(meta.get("output_width") or 0) or int(max(b.x_max for b in blocks) * 1.1)
    return format_ocr_blocks(blocks, img_height=img_h, img_width=img_w)


class RapidOCREngine:
    """Thread-safe lazy singleton wrapper around offline RapidOCR (ONNX Runtime)."""

    def __init__(self) -> None:
        self._init_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._engine: Any = None

    def _ensure_engine(self) -> Any:
        if self._engine is None:
            with self._init_lock:
                if self._engine is None:
                    from rapidocr_onnxruntime import RapidOCR

                    self._engine = RapidOCR()
        return self._engine

    def recognize(self, bgr: np.ndarray, *, low_conf_threshold: float = 0.82) -> OCRResult:
        if bgr is None or bgr.size == 0:
            return OCRResult(flags=["empty_image"])

        engine = self._ensure_engine()
        with self._infer_lock:
            raw_res, _ = engine(bgr)

        blocks: list[OCRBlock] = []
        if raw_res:
            for item in raw_res:
                if not item or len(item) < 3:
                    continue
                bbox_raw, text, score = item[0], str(item[1]), float(item[2])
                bbox = [[float(pt[0]), float(pt[1])] for pt in bbox_raw]
                if text.strip():
                    blocks.append(OCRBlock(bbox=bbox, text=text.strip(), confidence=score))

        if not blocks:
            return OCRResult(
                blocks=[],
                raw_text="",
                formatted_text="",
                avg_confidence=0.0,
                min_confidence=0.0,
                flags=["empty_ocr"],
            )

        total_chars = sum(max(1, len(b.text)) for b in blocks)
        avg_conf = sum(b.confidence * max(1, len(b.text)) for b in blocks) / float(total_chars)
        min_conf = min(b.confidence for b in blocks)
        raw_text = "\n".join(b.text for b in blocks)
        h, w = bgr.shape[:2]
        formatted_text = format_ocr_blocks(blocks, img_height=h, img_width=w)

        flags: list[str] = []
        if avg_conf < low_conf_threshold:
            flags.append("low_ocr_confidence")

        return OCRResult(
            blocks=blocks,
            raw_text=raw_text,
            formatted_text=formatted_text or raw_text,
            avg_confidence=float(avg_conf),
            min_confidence=float(min_conf),
            flags=flags,
        )
