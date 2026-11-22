"""L3 Optional OpenAI-compatible LLM proofreading & Markdown cleanup."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from book_cleaner.pipeline.ocr_engine import build_coordinate_anchor_text
from book_cleaner.pipeline.offline_ocr_hub import is_local_only_url


LLMMode = Literal["off", "auto_low_conf", "all"]

SYSTEM_PROMPT = (
    "你是严谨的纸质书籍与古籍 OCR 校对助手。"
    "请仅对输入的 OCR 文本进行明显错别字修正、误识别标点修复、断行合并与清晰 Markdown 排版整理。"
    "遇到章节小标题请用 ## 标题，遇到编号/项目符号请保留列表，遇到表格请务必使用标准 Markdown 表格语法（| 列1 | 列2 | 与 | --- | --- | 分隔行）。"
    "严禁增删正文事实、改写文风、总结扩写或编造原文不存在的内容。"
    "直接输出整理后的纯净 Markdown 文本。"
)


@dataclass(slots=True)
class LLMConfig:
    mode: LLMMode = "off"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    low_conf_threshold: float = 0.85
    timeout_s: float = 45.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "LLMConfig":
        if not data:
            return cls()
        mode = str(data.get("mode", "off"))
        if mode not in ("off", "auto_low_conf", "all"):
            mode = "off"
        return cls(
            mode=mode,  # type: ignore[arg-type]
            base_url=str(data.get("base_url", "https://api.openai.com/v1")).strip(),
            api_key=str(data.get("api_key", "")).strip(),
            model=str(data.get("model", "gpt-4o-mini")).strip() or "gpt-4o-mini",
            low_conf_threshold=float(data.get("low_conf_threshold", 0.85)),
            timeout_s=float(data.get("timeout_s", 45.0)),
        )


def _garbled_ratio(text: str) -> float:
    if not text:
        return 0.0
    weird = sum(1 for ch in text if ch == "\ufffd")
    return weird / float(len(text))


def should_run_llm(cfg: LLMConfig, ocr_confidence: float, text: str) -> bool:
    """Gate LLM proofreading according to configured mode and OCR quality."""
    if cfg.mode == "off":
        return False
    if not cfg.base_url or not cfg.api_key:
        return False
    if not text or not text.strip():
        return False
    if cfg.mode == "all":
        return True
    # auto_low_conf
    if ocr_confidence < cfg.low_conf_threshold:
        return True
    if _garbled_ratio(text) > 0.01:
        return True
    return False


def _strip_code_fence(content: str) -> str:
    stripped = content.strip()
    m = re.match(r"^```(?:json|markdown|md|text)?\s*\n(.*?)\n```$", stripped, flags=re.DOTALL)
    if m:
        return m.group(1).strip()
    return stripped


def clean_text_with_llm(
    text: str,
    cfg: LLMConfig,
    client: httpx.Client | None = None,
) -> tuple[str, dict[str, Any]]:
    """Proofread OCR text via any OpenAI-compatible Chat Completions endpoint."""
    if not cfg.base_url or not cfg.api_key:
        raise ValueError("LLM base_url and api_key are required")

    url = f"{cfg.base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": cfg.model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }

    t0 = time.perf_counter()
    if client is not None:
        resp = client.post(url, json=payload, headers=headers, timeout=cfg.timeout_s)
    else:
        with httpx.Client(timeout=cfg.timeout_s) as own_client:
            resp = own_client.post(url, json=payload, headers=headers)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    resp.raise_for_status()
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("LLM response contained no choices")
    raw_content = str(choices[0].get("message", {}).get("content", "")).strip()
    cleaned = _strip_code_fence(raw_content) or text
    return cleaned, {
        "model": body.get("model", cfg.model),
        "usage": body.get("usage", {}),
        "latency_ms": elapsed_ms,
    }


def test_llm_connection(cfg: LLMConfig, client: httpx.Client | None = None) -> dict[str, Any]:
    """Verify connectivity & auth against configured OpenAI-compatible endpoint."""
    if not cfg.base_url or not cfg.api_key:
        return {"ok": False, "message": "请先填写 Base URL 和 API Key", "model": cfg.model, "latency_ms": 0}
    url = f"{cfg.base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": cfg.model,
        "max_tokens": 16,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": "请回复 OK"}],
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    t0 = time.perf_counter()
    try:
        if client is not None:
            resp = client.post(url, json=payload, headers=headers, timeout=min(cfg.timeout_s, 20.0))
        else:
            with httpx.Client(timeout=min(cfg.timeout_s, 20.0)) as own_client:
                resp = own_client.post(url, json=payload, headers=headers)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        resp.raise_for_status()
        return {
            "ok": True,
            "message": f"连接成功 ({latency_ms} ms)",
            "model": cfg.model,
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "ok": False,
            "message": f"连接失败: {exc}",
            "model": cfg.model,
            "latency_ms": latency_ms,
        }


def extract_sheet_rows_with_llm(
    page_text: str,
    sheet_def: dict[str, Any],
    cfg: LLMConfig,
    client: httpx.Client | None = None,
    *,
    ocr_blocks: list[Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract structured rows matching a target template Sheet via an OpenAI-compatible LLM."""
    if not cfg.base_url or (not cfg.api_key and not is_local_only_url(cfg.base_url)):
        raise ValueError("LLM base_url and api_key are required")
    fields = list(sheet_def.get("fields") or [])
    if not fields:
        raise ValueError("目标子表尚未配置任何字段，无法抽取")

    sheet_name = str(sheet_def.get("name") or "目标子表")
    sheet_mode = str(sheet_def.get("mode") or "record")
    field_keys = [str(f.get("key") or f.get("name")) for f in fields]
    field_specs = [
        {
            "key": str(f.get("key") or f.get("name")),
            "label": str(f.get("name") or f.get("key")),
            "role": str(f.get("role") or "num_feature"),
            "aliases": list(f.get("aliases") or []),
            "min_val": f.get("min_val"),
            "max_val": f.get("max_val"),
        }
        for f in fields
    ]

    anchor_text = build_coordinate_anchor_text(ocr_blocks) if ocr_blocks else ""
    sys_prompt = (
        "你是工业与科研纸质单据结构化抽取助手。"
        "严格依据输入的 OCR 文本，仅抽取目标 Excel 子表要求的字段；"
        "如输入包含 coordinate_anchors（[x0,y0,x1,y1] 文本框坐标骨架），请务必对照坐标行序与左右列位对齐无线表格数据，防止串行漏行与幻觉；"
        "严禁编造原文中不存在的数值，缺失字段一律填 null，数值不要带单位后缀（如 32.5℃ 只写 32.5）。"
        '仅输出严格合法的 JSON，格式为 {"rows": [{field_key: value_or_null, ...}]}。'
    )
    user_payload: dict[str, Any] = {
        "target_sheet": sheet_name,
        "sheet_mode": sheet_mode,
        "fields": field_specs,
        "ocr_text": page_text[:6000],
    }
    if anchor_text:
        user_payload["coordinate_anchors"] = anchor_text
    user_prompt = json.dumps(user_payload, ensure_ascii=False)

    url = f"{cfg.base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": cfg.model,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key or 'local'}",
        "Content-Type": "application/json",
    }

    t0 = time.perf_counter()
    if client is not None:
        resp = client.post(url, json=payload, headers=headers, timeout=cfg.timeout_s)
    else:
        with httpx.Client(timeout=cfg.timeout_s) as own_client:
            resp = own_client.post(url, json=payload, headers=headers)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    resp.raise_for_status()
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("LLM response contained no choices")
    raw_content = str(choices[0].get("message", {}).get("content", "")).strip()
    cleaned = _strip_code_fence(raw_content)

    # Extract JSON object or array safely even if wrapped in prose
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        m_obj = re.search(r"(\{.*\}|\[.*\])", cleaned, flags=re.DOTALL)
        if not m_obj:
            raise RuntimeError(f"LLM 未返回合法 JSON：{cleaned[:160]}")
        parsed = json.loads(m_obj.group(1))

    if isinstance(parsed, dict):
        raw_rows = parsed.get("rows")
        if raw_rows is None and any(k in parsed for k in field_keys):
            raw_rows = [parsed]
    elif isinstance(parsed, list):
        raw_rows = parsed
    else:
        raw_rows = []

    if not isinstance(raw_rows, list):
        raw_rows = []

    norm_rows: list[dict[str, Any]] = []
    name_to_key = {str(f.get("name") or ""): str(f.get("key") or "") for f in fields}
    for raw_r in raw_rows:
        if not isinstance(raw_r, dict):
            continue
        out_r: dict[str, Any] = {}
        has_any = False
        for k in field_keys:
            val = raw_r.get(k)
            if val is None:
                # Fallback if model returned field label instead of key
                label = next((str(f.get("name") or "") for f in fields if f.get("key") == k), "")
                if label and label in raw_r:
                    val = raw_r.get(label)
            if isinstance(val, str):
                val = val.strip() or None
            if val is not None:
                has_any = True
            out_r[k] = val
        # Also catch keys written by label
        for raw_k, raw_v in raw_r.items():
            mapped_k = name_to_key.get(str(raw_k).strip())
            if mapped_k and out_r.get(mapped_k) is None and raw_v is not None:
                v_clean = raw_v.strip() if isinstance(raw_v, str) else raw_v
                if v_clean not in ("", None):
                    out_r[mapped_k] = v_clean
                    has_any = True
        if has_any:
            norm_rows.append(out_r)

    return norm_rows, {
        "model": body.get("model", cfg.model),
        "usage": body.get("usage", {}),
        "latency_ms": elapsed_ms,
    }
