"""Smart multi-engine OCR router with strict-offline privacy enforcement and multi-tier local engines."""

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

from book_cleaner.pipeline.ocr_engine import (
    OCRBlock,
    OCRResult,
    RapidOCREngine,
    build_coordinate_anchor_text,
)
from book_cleaner.pipeline.offline_ocr_hub import _CATALOG_BY_ID, is_local_only_url


OCREngineMode = Literal[
    "rapidocr",
    "ppocrv5_mobile",
    "ppocrv5_server",
    "ollama_glm_ocr",
    "ollama_qwen25vl_3b",
    "deepseek_ocr2_local",
    "deepseek_ocr2_api",
    "hybrid_smart",
]

VALID_OCR_ENGINES = frozenset(
    {
        "rapidocr",
        "ppocrv5_mobile",
        "ppocrv5_server",
        "ollama_glm_ocr",
        "ollama_qwen25vl_3b",
        "deepseek_ocr2_local",
        "deepseek_ocr2_api",
        "hybrid_smart",
    }
)

VISION_OCR_PROMPT = (
    "你是高精度的纸质书籍与档案 OCR 引擎。"
    "请逐字精准识别图片中的所有文字与表格内容并输出清晰 Markdown："
    "1) 遇到表格请严格输出标准 Markdown 管道表格（| 列1 | 列2 |）；"
    "2) 章节标题用 ## 标记；"
    "3) 严禁遗漏、总结、翻译或编造原文不存在的文字。只输出正文。"
)


def build_anchored_vision_prompt(anchor_context: str = "") -> str:
    """Build an olmOCR-style coordinate-anchored prompt combining visual OCR rules and layout boxes."""
    anchor = (anchor_context or "").strip()
    if not anchor:
        return VISION_OCR_PROMPT
    return (
        f"{VISION_OCR_PROMPT}\n\n"
        "【Document Anchoring 版面坐标骨架（来自前置快速 OCR，仅作位置与行序参考）】\n"
        "格式为 `[x0,y0,x1,y1] 文本`；请严格对照下述坐标骨架对齐无线表格列位与逐行顺序，防止串行、漏行或幻觉编造；"
        "遇到手写数字、模糊字或小数点（例如 31.5℃ 与 315℃）时，务必以原图视觉为准进行精准纠偏：\n"
        f"{anchor}"
    )


@dataclass(slots=True)
class OCRSettings:
    engine: OCREngineMode = "rapidocr"
    strict_offline: bool = True
    ollama_base_url: str = "http://127.0.0.1:11434"
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
        if engine not in VALID_OCR_ENGINES:
            engine = "rapidocr"
        strict_val = data.get("strict_offline", True)
        strict_offline = bool(strict_val) if isinstance(strict_val, bool) else str(strict_val).lower() not in ("0", "false", "no")
        ollama_url = str(data.get("ollama_base_url", "http://127.0.0.1:11434")).strip() or "http://127.0.0.1:11434"
        return cls(
            engine=engine,  # type: ignore[arg-type]
            strict_offline=strict_offline,
            ollama_base_url=ollama_url,
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


def _call_openai_vision_ocr(
    bgr: np.ndarray,
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_s: float,
    strict_offline: bool,
    client: httpx.Client | None = None,
    anchor_context: str = "",
    engine_used: str = "deepseek_ocr2_api",
) -> OCRResult:
    if strict_offline and not is_local_only_url(base_url):
        raise PermissionError("已开启涉密严格离线模式，已拦截外部云端 OCR 请求")

    b64 = _encode_bgr_to_base64_jpeg(bgr)
    prompt_text = build_anchored_vision_prompt(anchor_context)
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key or 'ollama'}",
        "Content-Type": "application/json",
    }

    if client is not None:
        resp = client.post(url, json=payload, headers=headers, timeout=timeout_s)
    else:
        with httpx.Client(timeout=timeout_s, trust_env=not is_local_only_url(base_url)) as own_client:
            resp = own_client.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("Vision OCR API returned no choices")
    content = _strip_markdown_fence(str(choices[0].get("message", {}).get("content", "")))
    if not content:
        return OCRResult(flags=["empty_ocr"], engine_used=engine_used)

    return OCRResult(
        blocks=[],
        raw_text=content,
        formatted_text=content,
        avg_confidence=0.92,
        min_confidence=0.92,
        flags=[],
        engine_used=engine_used,
    )


def recognize_with_deepseek_api(
    bgr: np.ndarray,
    cfg: OCRSettings,
    client: httpx.Client | None = None,
    *,
    anchor_context: str = "",
) -> OCRResult:
    """Call OpenAI-compatible Vision endpoint (blocked unless local when strict_offline=True)."""
    if cfg.strict_offline and not is_local_only_url(cfg.api_base_url):
        raise PermissionError("已开启涉密严格离线模式，禁止访问外部云端 Vision API")
    if not cfg.api_base_url or (not cfg.api_key and not is_local_only_url(cfg.api_base_url)):
        raise ValueError("DeepSeek Vision API base_url and api_key are required")

    return _call_openai_vision_ocr(
        bgr,
        base_url=cfg.api_base_url,
        api_key=cfg.api_key or "local",
        model=cfg.api_model,
        timeout_s=cfg.timeout_s,
        strict_offline=cfg.strict_offline,
        client=client,
        anchor_context=anchor_context,
        engine_used="deepseek_ocr2_api",
    )


def recognize_with_ollama_vision(
    bgr: np.ndarray,
    *,
    model_name: str,
    base_url: str = "http://127.0.0.1:11434",
    timeout_s: float = 90.0,
    client: httpx.Client | None = None,
    anchor_context: str = "",
    engine_used: str = "ollama_glm_ocr",
) -> OCRResult:
    """Run local Ollama Vision OCR (GLM-OCR 0.9B / Qwen2.5-VL 3B) strictly over loopback."""
    if not is_local_only_url(base_url):
        raise PermissionError("本机离线 Ollama 地址必须是 127.0.0.1 / localhost")
    openai_v1 = f"{base_url.rstrip('/')}/v1"
    return _call_openai_vision_ocr(
        bgr,
        base_url=openai_v1,
        api_key="ollama",
        model=model_name,
        timeout_s=timeout_s,
        strict_offline=True,
        client=client,
        anchor_context=anchor_context,
        engine_used=engine_used,
    )


def test_deepseek_api_connection(
    cfg: OCRSettings,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Verify connectivity and auth against a Vision API endpoint while honoring strict_offline."""
    if cfg.strict_offline and not is_local_only_url(cfg.api_base_url):
        return {
            "ok": False,
            "message": "当前已开启「涉密严格离线模式」，仅允许连接本机 127.0.0.1 / localhost 地址",
            "model": cfg.api_model,
            "latency_ms": 0,
        }
    if not cfg.api_base_url or (not cfg.api_key and not is_local_only_url(cfg.api_base_url)):
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
        return {"ok": True, "message": f"视觉接口连接成功 ({ms} ms)", "model": cfg.api_model, "latency_ms": ms}
    except Exception as exc:
        ms = int((time.perf_counter() - t0) * 1000)
        return {"ok": False, "message": f"连接失败: {exc}", "model": cfg.api_model, "latency_ms": ms}


_NUMERIC_TOKEN_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")
_INLINE_TEMP_OOB_RE = re.compile(r"(?:温度|罐温|Temp)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:℃|°C)?", re.IGNORECASE)
_INLINE_PH_OOB_RE = re.compile(r"(?:酸碱度|pH值?)\s*[:：]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_INLINE_PCT_OOB_RE = re.compile(r"(?:溶氧|DO|纯度|收率|得率|含量)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)


def _group_visual_rows(blocks: list[OCRBlock]) -> list[list[OCRBlock]]:
    if not blocks:
        return []
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
    return rows


def _looks_like_complex_table(blocks: list[OCRBlock]) -> bool:
    """Heuristic detecting multi-column tabular layout from RapidOCR spatial blocks."""
    if len(blocks) < 4:
        return False
    med_h = sorted(b.height for b in blocks)[len(blocks) // 2]
    rows = _group_visual_rows(blocks)

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


def _has_high_line_fragmentation(blocks: list[OCRBlock]) -> bool:
    """Detect borderless tables or handwritten pages shattered into many tiny fragments per line."""
    if len(blocks) < 8:
        return False
    rows = _group_visual_rows(blocks)
    if len(rows) < 2:
        return False
    avg_blocks_per_row = len(blocks) / float(len(rows))
    short_blocks = sum(1 for b in blocks if len(b.text.strip()) <= 6)
    short_ratio = short_blocks / float(len(blocks))
    return avg_blocks_per_row >= 3.2 or (short_ratio >= 0.65 and avg_blocks_per_row >= 2.4)


def _has_numeric_density_anomaly(blocks: list[OCRBlock], text: str) -> bool:
    """Detect numeric-dense pages with low numeric confidence or suspect out-of-range digits."""
    combined_text = text or "\n".join(b.text for b in blocks)
    for m in _INLINE_TEMP_OOB_RE.finditer(combined_text):
        if float(m.group(1)) > 100.0:
            return True
    for m in _INLINE_PH_OOB_RE.finditer(combined_text):
        if float(m.group(1)) > 14.0:
            return True
    for m in _INLINE_PCT_OOB_RE.finditer(combined_text):
        if float(m.group(1)) > 100.0:
            return True

    if not blocks:
        return False
    num_blocks = [b for b in blocks if _NUMERIC_TOKEN_RE.search(b.text)]
    if len(num_blocks) < 4:
        return False
    density = len(num_blocks) / float(len(blocks))
    avg_num_conf = sum(b.confidence for b in num_blocks) / float(len(num_blocks))
    return (len(num_blocks) >= 6 or density >= 0.35) and avg_num_conf < 0.88


def evaluate_hybrid_upgrade_triggers(
    fast_res: OCRResult,
    cfg: OCRSettings,
    *,
    extra_range_alerts: list[str] | None = None,
) -> list[str]:
    """Return ordered trigger reasons indicating why a page benefits from VLM upgrade."""
    reasons: list[str] = []
    if fast_res.avg_confidence < cfg.hybrid_conf_threshold:
        reasons.append("low_confidence")
    if cfg.hybrid_upgrade_tables and _looks_like_complex_table(fast_res.blocks):
        reasons.append("complex_table")
    text = fast_res.formatted_text or fast_res.raw_text
    if _has_numeric_density_anomaly(fast_res.blocks, text):
        reasons.append("numeric_density_anomaly")
    if _has_high_line_fragmentation(fast_res.blocks):
        reasons.append("high_line_fragmentation")
    if extra_range_alerts:
        reasons.append("out_of_range_alert")
    return reasons


class SmartOCRRouter:
    """Routes OCR across offline ONNX weights, local Ollama VLMs, DeepSeek-OCR 2 CUDA, and opt-in API."""

    def __init__(
        self,
        db: Any,
        addon_manager: Any,
        *,
        offline_hub: Any | None = None,
        rapid_engine: RapidOCREngine | None = None,
        api_client: httpx.Client | None = None,
    ) -> None:
        self.db = db
        self.addon_manager = addon_manager
        self.offline_hub = offline_hub
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
                return OCRResult(flags=["empty_ocr"], engine_used="deepseek_ocr2_local")
            return OCRResult(
                blocks=[],
                raw_text=cleaned,
                formatted_text=cleaned,
                avg_confidence=0.93,
                min_confidence=0.93,
                flags=[],
                engine_used="deepseek_ocr2_local",
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def _run_best_fast_onnx(self, bgr: np.ndarray, *, low_conf_threshold: float) -> OCRResult:
        if self.offline_hub is not None:
            for mid in ("ppocrv5_server", "ppocrv5_mobile"):
                eng = self.offline_hub.get_onnx_engine(mid)
                if eng is not None:
                    try:
                        res = eng.recognize(bgr, low_conf_threshold=low_conf_threshold)
                        res.engine_used = mid
                        return res
                    except Exception:
                        pass
        res = self.rapid_engine.recognize(bgr, low_conf_threshold=low_conf_threshold)
        if not getattr(res, "engine_used", ""):
            res.engine_used = "rapidocr"
        return res

    def _get_anchor_pass(
        self, bgr: np.ndarray, *, low_conf_threshold: float
    ) -> tuple[OCRResult | None, str]:
        try:
            anchor_res = self._run_best_fast_onnx(bgr, low_conf_threshold=low_conf_threshold)
            return anchor_res, build_coordinate_anchor_text(anchor_res.blocks)
        except Exception:
            return None, ""

    def _try_vision_upgrade(
        self,
        bgr: np.ndarray,
        cfg: OCRSettings,
        *,
        anchor_context: str = "",
    ) -> OCRResult | None:
        # 1. Local DeepSeek-OCR 2 CUDA add-on
        if self.addon_manager is not None and self.addon_manager.is_installed():
            try:
                return self._run_deepseek_local(bgr)
            except Exception:
                pass
        # 2. Local Ollama compact vision OCR (GLM-OCR 0.9B > Qwen2.5-VL 3B)
        if self.offline_hub is not None and is_local_only_url(cfg.ollama_base_url):
            for mid in ("ollama_glm_ocr", "ollama_qwen25vl_3b"):
                cat = _CATALOG_BY_ID.get(mid)
                if cat and self.offline_hub.is_model_installed(mid):
                    try:
                        return recognize_with_ollama_vision(
                            bgr,
                            model_name=str(cat["ollama_model"]),
                            base_url=cfg.ollama_base_url,
                            timeout_s=max(60.0, cfg.timeout_s),
                            client=self.api_client,
                            anchor_context=anchor_context,
                            engine_used=mid,
                        )
                    except Exception:
                        pass
        # 3. Vision API (ONLY when strict_offline=False OR URL is strictly local loopback)
        if cfg.api_base_url and (not cfg.strict_offline or is_local_only_url(cfg.api_base_url)):
            if cfg.api_key or is_local_only_url(cfg.api_base_url):
                try:
                    return recognize_with_deepseek_api(
                        bgr,
                        cfg,
                        client=self.api_client,
                        anchor_context=anchor_context,
                    )
                except Exception:
                    pass
        return None

    def recognize(
        self,
        bgr: np.ndarray,
        *,
        low_conf_threshold: float = 0.82,
        extra_range_alerts: list[str] | None = None,
    ) -> OCRResult:
        settings = self.db.get_settings() if self.db is not None else {}
        cfg = OCRSettings.from_dict(settings.get("ocr"))

        if cfg.engine == "rapidocr":
            res = self.rapid_engine.recognize(bgr, low_conf_threshold=low_conf_threshold)
            res.engine_used = "rapidocr"
            return res

        if cfg.engine in ("ppocrv5_mobile", "ppocrv5_server"):
            if self.offline_hub is not None:
                eng = self.offline_hub.get_onnx_engine(cfg.engine)
                if eng is not None:
                    try:
                        res = eng.recognize(bgr, low_conf_threshold=low_conf_threshold)
                        res.engine_used = cfg.engine
                        return res
                    except Exception:
                        pass
            fallback = self.rapid_engine.recognize(bgr, low_conf_threshold=low_conf_threshold)
            fallback.engine_used = "rapidocr"
            if "ocr_fallback_rapidocr" not in fallback.flags:
                fallback.flags.append("ocr_fallback_rapidocr")
            return fallback

        if cfg.engine in ("ollama_glm_ocr", "ollama_qwen25vl_3b"):
            cat = _CATALOG_BY_ID.get(cfg.engine)
            if cat and is_local_only_url(cfg.ollama_base_url):
                anchor_res, anchor_ctx = self._get_anchor_pass(bgr, low_conf_threshold=low_conf_threshold)
                try:
                    res = recognize_with_ollama_vision(
                        bgr,
                        model_name=str(cat["ollama_model"]),
                        base_url=cfg.ollama_base_url,
                        timeout_s=max(60.0, cfg.timeout_s),
                        client=self.api_client,
                        anchor_context=anchor_ctx,
                        engine_used=cfg.engine,
                    )
                    if not res.blocks and anchor_res is not None and anchor_res.blocks:
                        res.blocks = anchor_res.blocks
                    return res
                except Exception:
                    pass
            fallback = self._run_best_fast_onnx(bgr, low_conf_threshold=low_conf_threshold)
            if "ocr_fallback_rapidocr" not in fallback.flags:
                fallback.flags.append("ocr_fallback_rapidocr")
            return fallback

        if cfg.engine == "deepseek_ocr2_local":
            if self.addon_manager is not None and self.addon_manager.is_installed():
                try:
                    return self._run_deepseek_local(bgr)
                except Exception:
                    pass
            fallback = self._run_best_fast_onnx(bgr, low_conf_threshold=low_conf_threshold)
            if "ocr_fallback_rapidocr" not in fallback.flags:
                fallback.flags.append("ocr_fallback_rapidocr")
            return fallback

        if cfg.engine == "deepseek_ocr2_api":
            if cfg.strict_offline and not is_local_only_url(cfg.api_base_url):
                fallback = self._run_best_fast_onnx(bgr, low_conf_threshold=low_conf_threshold)
                if "ocr_blocked_offline_policy" not in fallback.flags:
                    fallback.flags.append("ocr_blocked_offline_policy")
                return fallback
            if cfg.api_base_url and (cfg.api_key or is_local_only_url(cfg.api_base_url)):
                anchor_res, anchor_ctx = self._get_anchor_pass(bgr, low_conf_threshold=low_conf_threshold)
                try:
                    res = recognize_with_deepseek_api(
                        bgr,
                        cfg,
                        client=self.api_client,
                        anchor_context=anchor_ctx,
                    )
                    if not res.blocks and anchor_res is not None and anchor_res.blocks:
                        res.blocks = anchor_res.blocks
                    return res
                except Exception:
                    pass
            fallback = self._run_best_fast_onnx(bgr, low_conf_threshold=low_conf_threshold)
            if "ocr_fallback_rapidocr" not in fallback.flags:
                fallback.flags.append("ocr_fallback_rapidocr")
            return fallback

        # hybrid_smart: best local ONNX first, upgrade when triggers fire to VLM with Document Anchoring
        fast_res = self._run_best_fast_onnx(bgr, low_conf_threshold=low_conf_threshold)
        reasons = evaluate_hybrid_upgrade_triggers(
            fast_res,
            cfg,
            extra_range_alerts=extra_range_alerts,
        )
        if reasons:
            anchor_ctx = build_coordinate_anchor_text(fast_res.blocks)
            upgraded = self._try_vision_upgrade(bgr, cfg, anchor_context=anchor_ctx)
            if upgraded is not None and upgraded.formatted_text.strip():
                if not upgraded.blocks and fast_res.blocks:
                    upgraded.blocks = fast_res.blocks
                upgraded.upgrade_reasons = reasons
                return upgraded
            fast_res.upgrade_reasons = reasons
        return fast_res
