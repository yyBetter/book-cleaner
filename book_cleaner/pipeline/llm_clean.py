"""L3 Optional OpenAI-compatible LLM proofreading & Markdown cleanup."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx


LLMMode = Literal["off", "auto_low_conf", "all"]

SYSTEM_PROMPT = (
    "你是严谨的纸质书籍与古籍 OCR 校对助手。"
    "请仅对输入的 OCR 文本进行明显错别字修正、误识别标点修复、断行合并与清晰 Markdown 排版整理。"
    "严禁增删正文事实、改写文风、总结扩写或编造原文不存在的内容。"
    "直接输出整理后的纯净文本。"
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
    m = re.match(r"^```(?:markdown|md|text)?\s*\n(.*?)\n```$", stripped, flags=re.DOTALL)
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
