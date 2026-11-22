"""L2 Offline OCR Engine (RapidOCR ONNX Runtime) and layout paragraph formatter."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
_PAGE_NUM_RE = re.compile(r"^[-—·~\[\]（）()\s]*\d{1,5}[-—·~\[\]（）()\s]*$|^第?\s*\d{1,4}\s*页$")
_CHAPTER_HEADING_RE = re.compile(
    r"^(第[一二三四五六七八九十百千万零两0-9]+[章节回篇卷部节].{0,32}|Chapter\s+\d+.*)$",
    re.IGNORECASE,
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


def format_ocr_blocks(blocks: list[OCRBlock], img_height: int, img_width: int) -> str:
    """Group OCR boxes into reading-order lines, drop margin page numbers, reconstruct headings & paragraphs."""
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

    line_records: list[dict[str, Any]] = []
    for row in lines:
        row.sort(key=lambda b: b.x_min)
        text_parts: list[str] = []
        for idx, b in enumerate(row):
            if idx == 0:
                text_parts.append(b.text.strip())
            else:
                gap = b.x_min - row[idx - 1].x_max
                if gap > b.height * 1.1:
                    text_parts.append("  " + b.text.strip())
                else:
                    text_parts[-1] = _smart_join(text_parts[-1], b.text.strip())
        merged = "".join(text_parts).strip()
        if not merged:
            continue
        line_records.append(
            {
                "text": merged,
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

    paragraphs: list[str] = []
    current_para = ""
    prev_lr: dict[str, Any] | None = None

    for lr in line_records:
        txt = lr["text"]
        is_heading = (
            (lr["height"] >= med_h * 1.26 and len(txt) <= 42 and txt[-1] not in "。！？，,.;；")
            or (_CHAPTER_HEADING_RE.match(txt) is not None and len(txt) <= 46)
        )

        if is_heading:
            if current_para:
                paragraphs.append(current_para)
                current_para = ""
            paragraphs.append(f"## {txt}")
            prev_lr = lr
            continue

        if prev_lr is None or not current_para:
            current_para = txt
            prev_lr = lr
            continue

        vert_gap = lr["y_min"] - prev_lr["y_max"]
        indented = lr["x_min"] > left_margin + max(med_h * 1.1, 0.03 * w)
        prev_short_terminal = (
            prev_lr["x_max"] < right_margin - 0.14 * text_span
            and prev_lr["text"][-1] in "。！？；：.!?:”\"』」"
        )
        new_para = (vert_gap > 0.85 * med_h) or indented or prev_short_terminal

        if new_para:
            paragraphs.append(current_para)
            current_para = txt
        else:
            current_para = _smart_join(current_para, txt)
        prev_lr = lr

    if current_para:
        paragraphs.append(current_para)

    return "\n\n".join(paragraphs)


class RapidOCREngine:
    """Thread-safe lazy singleton wrapper around offline RapidOCR (ONNX Runtime)."""

    def __init__(
        self,
        *,
        det_model_path: str | Path | None = None,
        rec_model_path: str | Path | None = None,
    ) -> None:
        self.det_model_path = Path(det_model_path) if det_model_path else None
        self.rec_model_path = Path(rec_model_path) if rec_model_path else None
        self._init_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._engine: Any = None

    def _ensure_engine(self) -> Any:
        if self._engine is None:
            with self._init_lock:
                if self._engine is None:
                    from rapidocr_onnxruntime import RapidOCR

                    kwargs: dict[str, Any] = {}
                    if self.det_model_path is not None:
                        kwargs["det_model_path"] = str(self.det_model_path)
                    if self.rec_model_path is not None:
                        kwargs["rec_model_path"] = str(self.rec_model_path)
                    self._engine = RapidOCR(**kwargs)
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
