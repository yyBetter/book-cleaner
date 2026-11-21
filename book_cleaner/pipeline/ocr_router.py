"""Smart multi-engine OCR router: RapidOCR baseline + DeepSeek-OCR 2 (Local Add-on / Vision API) + Auto-Fallback."""

from __future__ import annotations

import base64
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import cv2
import httpx
import numpy as np

from book_cleaner.pipeline.ocr_engine import OCRBlock, OCRResult, RapidOCREngine


OCREngineMode = Literal["rapidocr", "deepseek_ocr2_local", "deepseek_ocr2_api", "hybrid_smart"]

VISION_OCR_PROMPT = (
    "你是高精度的纸质书籍与档案 OCR 引擎。"
    "请逐字精准识别图片中的所有文字与表格内容并输出清晰 Markdown："
    "1) 遇到表格请严格输出标准 Markdown 管道表格（| 列1 | 列2 |）；"
    "2) 章节标题用 ## 标记；"
    "3) 严禁遗漏、总结、翻译或编造原文不存在的文字。只输出正文。"
)


@dataclass(slots=True)
class OCRSettings:
    engine: OCREngineMode = "rapidocr"
    api_base_url: str = "https://api.siliconflow.cn/v1"
    api_key: str = ""
    api_model: str = "deepseek-ai/DeepSeek-OCR-2"
    hybrid_conf_threshold: float = 0.85
    hybrid_upgrade_tables: bool = True
    timeout_s: float = 60.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "OCRSettings":
        if not data:
            return cls()
        engine = str(data.get("engine", "rapidocr"))
        if engine not in ("rapidocr", "deepseek_ocr2_local", "deepseek_ocr2_api", "hybrid_smart"):
            engine = "rapidocr"
        return cls(
            engine=engine,  # type: ignore[arg-type]
            api_base_url=str(data.get("api_base_url", "https://api.siliconflow.cn/v1")).strip(),
            api_key=str(data.get("api_key", "")).strip(),
            api_model=str(data.get("api_model", "deepseek-ai/DeepSeek-OCR-2")).strip()
            or "deepseek-ai/DeepSeek-OCR-2",
            hybrid_conf_threshold=float(data.get("hybrid_conf_threshold", 0.85)),
            hybrid_upgrade_tables=bool(data.get("hybrid_upgrade_tables", True)),
            timeout_s=float(data.get("timeout_s", 60.0)),
        )


def _strip_markdown_fence(text: str) -> str:
    s = text.strip()
    m = re.match(r"^```(?:markdown|md|text)?\s*\n(.*?)\n```$", s, flags=re.DOTALL)
    return m.group(1).strip() if m else s


def _encode_bgr_to_base64_jpeg(bgr: np.ndarray, max_dim: int = 2200) -> str:
    h, w = bgr.shape[:2]
    img = bgr
    if max(h, w) > max_dim and max(h, w) > 0:
        scale = max_dim / float(max(h, w))
        img = cv2.resize(
            bgr,
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        raise RuntimeError("Failed to encode image for Vision OCR")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def recognize_with_deepseek_api(
    bgr: np.ndarray,
    cfg: OCRSettings,
    client: httpx.Client | None = None,
) -> OCRResult:
    """Call OpenAI-compatible Vision endpoint serving DeepSeek-OCR 2."""
    if not cfg.api_base_url or not cfg.api_key:
        raise ValueError("DeepSeek Vision API base_url and api_key are required")

    b64 = _encode_bgr_to_base64_jpeg(bgr)
    url = f"{cfg.api_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": cfg.api_model,
        "temperature": 0.0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }

    if client is not None:
        resp = client.post(url, json=payload, headers=headers, timeout=cfg.timeout_s)
    else:
        with httpx.Client(timeout=cfg.timeout_s) as own_client:
            resp = own_client.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("Vision OCR API returned no choices")
    content = _strip_markdown_fence(str(choices[0].get("message", {}).get("content", "")))
    if not content:
        return OCRResult(flags=["empty_ocr"])

    return OCRResult(
        blocks=[],
        raw_text=content,
        formatted_text=content,
        avg_confidence=0.92,
        min_confidence=0.92,
        flags=[],
    )


def test_deepseek_api_connection(
    cfg: OCRSettings,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Verify connectivity and auth against a DeepSeek-OCR 2 Vision API endpoint."""
    if not cfg.api_base_url or not cfg.api_key:
        return {
            "ok": False,
            "message": "请先填写 Vision API Base URL 和 API Key",
            "model": cfg.api_model,
            "latency_ms": 0,
        }
    tiny = np.full((64, 160, 3), 255, dtype=np.uint8)
    cv2.putText(tiny, "OK", (20, 46), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 2)
    t0 = time.perf_counter()
    try:
        recognize_with_deepseek_api(tiny, cfg, client=client)
        ms = int((time.perf_counter() - t0) * 1000)
        return {"ok": True, "message": f"DeepSeek 视觉 API 连接成功 ({ms} ms)", "model": cfg.api_model, "latency_ms": ms}
    except Exception as exc:
        ms = int((time.perf_counter() - t0) * 1000)
        return {"ok": False, "message": f"连接失败: {exc}", "model": cfg.api_model, "latency_ms": ms}


def _looks_like_complex_table(blocks: list[OCRBlock]) -> bool:
    """Heuristic detecting multi-column tabular layout from RapidOCR spatial blocks."""
    if len(blocks) < 4:
        return False
    sorted_b = sorted(blocks, key=lambda b: (b.y_center, b.x_min))
    med_h = sorted(b.height for b in sorted_b)[len(sorted_b) // 2]

    rows: list[list[OCRBlock]] = []
    for b in sorted_b:
        placed = False
        for row in reversed(rows[-3:]):
            row_yc = sum(r.y_center for r in row) / len(row)
            if abs(b.y_center - row_yc) <= 0.55 * med_h:
                row.append(b)
                placed = True
                break
        if not placed:
            rows.append([b])

    multi_col_rows = 0
    for row in rows:
        row.sort(key=lambda b: b.x_min)
        sep_cells = 1
        for i in range(1, len(row)):
            if (row[i].x_min - row[i - 1].x_max) > med_h * 1.35:
                sep_cells += 1
        if sep_cells >= 2:
            multi_col_rows += 1
    return multi_col_rows >= 2


class SmartOCRRouter:
    """Routes OCR across built-in RapidOCR and optional DeepSeek-OCR 2 (local add-on or Vision API)."""

    def __init__(
        self,
        db: Any,
        addon_manager: Any,
        *,
        rapid_engine: RapidOCREngine | None = None,
        api_client: httpx.Client | None = None,
    ) -> None:
        self.db = db
        self.addon_manager = addon_manager
        self.rapid_engine = rapid_engine if rapid_engine is not None else RapidOCREngine()
        self.api_client = api_client

    def _run_deepseek_local(self, bgr: np.ndarray) -> OCRResult:
        ok, png_buf = cv2.imencode(".png", bgr)
        if not ok:
            raise RuntimeError("Failed to encode temp PNG for local DeepSeek add-on")
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                tf.write(png_buf.tobytes())
                tmp_path = Path(tf.name)
            text = self.addon_manager.run_local_inference(tmp_path)
            cleaned = _strip_markdown_fence(text)
            if not cleaned:
                return OCRResult(flags=["empty_ocr"])
            return OCRResult(
                blocks=[],
                raw_text=cleaned,
                formatted_text=cleaned,
                avg_confidence=0.93,
                min_confidence=0.93,
                flags=[],
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def _try_deepseek_upgrade(self, bgr: np.ndarray, cfg: OCRSettings) -> OCRResult | None:
        if self.addon_manager is not None and self.addon_manager.is_installed():
            try:
                return self._run_deepseek_local(bgr)
            except Exception:
                pass
        if cfg.api_base_url and cfg.api_key:
            try:
                return recognize_with_deepseek_api(bgr, cfg, client=self.api_client)
            except Exception:
                pass
        return None

    def recognize(self, bgr: np.ndarray, *, low_conf_threshold: float = 0.82) -> OCRResult:
        settings = self.db.get_settings() if self.db is not None else {}
        cfg = OCRSettings.from_dict(settings.get("ocr"))

        if cfg.engine == "rapidocr":
            return self.rapid_engine.recognize(bgr, low_conf_threshold=low_conf_threshold)

        if cfg.engine == "deepseek_ocr2_local":
            if self.addon_manager is not None and self.addon_manager.is_installed():
                try:
                    return self._run_deepseek_local(bgr)
                except Exception:
                    pass
            fallback = self.rapid_engine.recognize(bgr, low_conf_threshold=low_conf_threshold)
            if "ocr_fallback_rapidocr" not in fallback.flags:
                fallback.flags.append("ocr_fallback_rapidocr")
            return fallback

        if cfg.engine == "deepseek_ocr2_api":
            if cfg.api_base_url and cfg.api_key:
                try:
                    return recognize_with_deepseek_api(bgr, cfg, client=self.api_client)
                except Exception:
                    pass
            fallback = self.rapid_engine.recognize(bgr, low_conf_threshold=low_conf_threshold)
            if "ocr_fallback_rapidocr" not in fallback.flags:
                fallback.flags.append("ocr_fallback_rapidocr")
            return fallback

        # hybrid_smart: RapidOCR first, upgrade low-conf or complex table pages to DeepSeek-OCR 2
        rapid_res = self.rapid_engine.recognize(bgr, low_conf_threshold=low_conf_threshold)
        need_upgrade = (rapid_res.avg_confidence < cfg.hybrid_conf_threshold) or (
            cfg.hybrid_upgrade_tables and _looks_like_complex_table(rapid_res.blocks)
        )
        if need_upgrade:
            upgraded = self._try_deepseek_upgrade(bgr, cfg)
            if upgraded is not None and upgraded.formatted_text.strip():
                return upgraded
        return rapid_res
