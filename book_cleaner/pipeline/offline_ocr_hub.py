"""Strict-offline confidential OCR model catalog, host hardware advisor, and pause/resume download manager."""

from __future__ import annotations

import json
import os
import platform
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
import psutil

from book_cleaner.pipeline.deepseek_addon import (
    DeepSeekAddonManager,
    inspect_hardware_for_deepseek,
    probe_nvidia_gpus,
)
from book_cleaner.pipeline.ocr_engine import RapidOCREngine


_LOCAL_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_local_only_url(url: str) -> bool:
    """Return True strictly for loopback URLs so confidential documents never leave the host PC."""
    raw = str(url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower()
    return host in _LOCAL_LOOPBACK_HOSTS


OFFLINE_OCR_CATALOG: list[dict[str, Any]] = [
    {
        "model_id": "rapidocr_builtin",
        "name": "RapidOCR 极速离线版（内置 PP-OCRv4）",
        "engine_value": "rapidocr",
        "runner": "builtin_onnx",
        "size_mb": 0.0,
        "min_ram_gb": 2.0,
        "min_disk_gb": 0.0,
        "min_vram_mb": 0,
        "requires_ollama": False,
        "requires_cuda": False,
        "summary": "随软件内置、零下载即开即用，纯 CPU 秒扫清晰印刷体与规整单据。",
        "strengths": ["无需联网下载", "极低内存占用", "毫秒级出字"],
        "files": [],
    },
    {
        "model_id": "ppocrv5_mobile",
        "name": "PP-OCRv5 轻量离线增强包（纯 CPU 通用）",
        "engine_value": "ppocrv5_mobile",
        "runner": "onnx_download",
        "size_mb": 20.5,
        "min_ram_gb": 4.0,
        "min_disk_gb": 0.3,
        "min_vram_mb": 0,
        "requires_ollama": False,
        "requires_cuda": False,
        "summary": "Paddle 新一代 PP-OCRv5 轻量版，仅 ~21 MB，老旧办公本也能纯 CPU 丝滑跑，斜拍与小字更稳。",
        "strengths": ["仅 ~21 MB 轻量秒下", "纯 CPU 无需显卡", "支持断点暂停/续传"],
        "files": [
            {
                "role": "det",
                "filename": "ch_PP-OCRv5_det_mobile.onnx",
                "url_cn": "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/master/onnx/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx",
                "url_global": "https://hf-mirror.com/RapidAI/RapidOCR/resolve/main/onnx/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx",
            },
            {
                "role": "rec",
                "filename": "ch_PP-OCRv5_rec_mobile.onnx",
                "url_cn": "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/master/onnx/PP-OCRv5/rec/ch_PP-OCRv5_rec_mobile.onnx",
                "url_global": "https://hf-mirror.com/RapidAI/RapidOCR/resolve/main/onnx/PP-OCRv5/rec/ch_PP-OCRv5_rec_mobile.onnx",
            },
        ],
    },
    {
        "model_id": "ppocrv5_server",
        "name": "PP-OCRv5 高精度离线权重包（无独显推荐）",
        "engine_value": "ppocrv5_server",
        "runner": "onnx_download",
        "size_mb": 164.7,
        "min_ram_gb": 8.0,
        "min_disk_gb": 0.8,
        "min_vram_mb": 0,
        "requires_ollama": False,
        "requires_cuda": False,
        "summary": "PP-OCRv5 大容量服务端权重，无需显卡、无需装 Ollama，复杂印刷表格与模糊笔迹精度大幅高于内置 v4。",
        "strengths": ["零外部运行时依赖", "纯 CPU 高精度版面", "支持断点暂停/续传"],
        "files": [
            {
                "role": "det",
                "filename": "ch_PP-OCRv5_det_server.onnx",
                "url_cn": "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/master/onnx/PP-OCRv5/det/ch_PP-OCRv5_det_server.onnx",
                "url_global": "https://hf-mirror.com/RapidAI/RapidOCR/resolve/main/onnx/PP-OCRv5/det/ch_PP-OCRv5_det_server.onnx",
            },
            {
                "role": "rec",
                "filename": "ch_PP-OCRv5_rec_server.onnx",
                "url_cn": "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/master/onnx/PP-OCRv5/rec/ch_PP-OCRv5_rec_server.onnx",
                "url_global": "https://hf-mirror.com/RapidAI/RapidOCR/resolve/main/onnx/PP-OCRv5/rec/ch_PP-OCRv5_rec_server.onnx",
            },
        ],
    },
    {
        "model_id": "ollama_glm_ocr",
        "name": "GLM-OCR 0.9B 轻量离线视觉版（表格/手写小钢炮）",
        "engine_value": "ollama_glm_ocr",
        "runner": "ollama_pull",
        "ollama_model": "glm-ocr",
        "size_mb": 2200.0,
        "min_ram_gb": 8.0,
        "min_disk_gb": 4.0,
        "min_vram_mb": 0,
        "requires_ollama": True,
        "requires_cuda": False,
        "summary": "仅 0.9B 参数视觉大模型，经本机 Ollama 全离线推理，Mac 统一内存与无独显电脑也能看懂复杂表格与手写体。",
        "strengths": ["0.9B 低门槛跑得动", "复杂表格直接转 Markdown", "中文手写大幅提升"],
        "files": [],
    },
    {
        "model_id": "ollama_qwen25vl_3b",
        "name": "Qwen2.5-VL 3B 离线视觉增强版（复杂手写语义）",
        "engine_value": "ollama_qwen25vl_3b",
        "runner": "ollama_pull",
        "ollama_model": "qwen2.5vl:3b",
        "size_mb": 3200.0,
        "min_ram_gb": 16.0,
        "min_disk_gb": 6.0,
        "min_vram_mb": 0,
        "requires_ollama": True,
        "requires_cuda": False,
        "summary": "3B 多模态大模型，结合上下文理解涂改、连笔手写和混排单据，适合 16GB+ Mac 或带独显主机。",
        "strengths": ["上下文语义纠错", "潦草手写涂改友好", "全离线不联网"],
        "files": [],
    },
    {
        "model_id": "deepseek_ocr2_local",
        "name": "DeepSeek-OCR 2 旗舰 CUDA 本地包",
        "engine_value": "deepseek_ocr2_local",
        "runner": "deepseek_addon",
        "size_mb": 12000.0,
        "min_ram_gb": 16.0,
        "min_disk_gb": 12.0,
        "min_vram_mb": 8192,
        "requires_ollama": False,
        "requires_cuda": True,
        "summary": "原生 PyTorch + CUDA 旗舰版，适合配备 ≥ 8GB 显存 NVIDIA 独显的工作站，大批量复杂文档旗舰精度。",
        "strengths": ["旗舰版面/表格还原", "独立 Python 隔离环境", "支持暂停保留断点"],
        "files": [],
    },
]

_CATALOG_BY_ID: dict[str, dict[str, Any]] = {m["model_id"]: m for m in OFFLINE_OCR_CATALOG}


def _existing_parent(path: Path) -> Path:
    cur = Path(path).resolve()
    while not cur.exists() and cur.parent != cur:
        cur = cur.parent
    return cur


def probe_ollama_local(
    base_url: str = "http://127.0.0.1:11434",
    *,
    timeout_s: float = 0.8,
    http_transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Probe local Ollama daemon and installed model tags without ever contacting external hosts."""
    installed_cli = bool(shutil.which("ollama")) or (
        platform.system() == "Darwin" and Path("/Applications/Ollama.app").exists()
    )
    running = False
    models: list[str] = []
    if is_local_only_url(base_url):
        tags_url = f"{base_url.rstrip('/')}/api/tags"
        try:
            with httpx.Client(transport=http_transport, timeout=timeout_s) as client:
                resp = client.get(tags_url)
                if resp.status_code == 200:
                    running = True
                    installed_cli = True
                    payload = resp.json()
                    for m in payload.get("models") or []:
                        name = str(m.get("name") or "").strip()
                        if name:
                            models.append(name)
        except Exception:
            pass
    return {
        "installed": installed_cli,
        "running": running,
        "base_url": base_url,
        "models": models,
    }


def inspect_host_hardware(
    data_dir: Path,
    *,
    ollama_base_url: str = "http://127.0.0.1:11434",
    gpu_probe: Callable[[], list[dict[str, Any]]] | None = None,
    disk_probe: Callable[[Path], float] | None = None,
    ram_probe: Callable[[], float] | None = None,
    uv_probe: Callable[[], str | None] | None = None,
    ollama_probe: Callable[[], dict[str, Any]] | None = None,
    platform_system: str | None = None,
    machine_arch: str | None = None,
    cpu_count: int | None = None,
) -> dict[str, Any]:
    """Inspect CPU, RAM, NVIDIA CUDA GPU, free disk space, uv, and local Ollama availability."""
    sys_name = platform_system or platform.system()
    arch = (machine_arch or platform.machine() or "").lower()
    cores = int(cpu_count or os.cpu_count() or 1)
    is_apple_silicon = sys_name == "Darwin" and arch in ("arm64", "aarch64")

    gpus = (gpu_probe or probe_nvidia_gpus)()
    max_vram_mb = max((int(g.get("vram_mb", 0)) for g in gpus), default=0)

    if disk_probe is not None:
        free_disk_gb = round(float(disk_probe(data_dir)), 2)
    else:
        target = _existing_parent(data_dir)
        free_disk_gb = round(shutil.disk_usage(target).free / float(1024**3), 2)

    if ram_probe is not None:
        ram_gb = round(float(ram_probe()), 2)
    else:
        ram_gb = round(psutil.virtual_memory().total / float(1024**3), 2)

    uv_bin = (uv_probe or (lambda: shutil.which("uv")))()
    ollama_info = (ollama_probe or (lambda: probe_ollama_local(ollama_base_url)))()

    gpu_label = f"{gpus[0]['name']} ({max_vram_mb} MB)" if gpus else (
        "Apple Silicon 统一内存 GPU" if is_apple_silicon else "无 NVIDIA CUDA 独显"
    )
    ollama_label = (
        f"运行中 ({len(ollama_info.get('models') or [])} 个本地模型)"
        if ollama_info.get("running")
        else ("已安装未启动" if ollama_info.get("installed") else "未安装")
    )
    summary = (
        f"系统 {sys_name} ({arch or '-'}) · CPU {cores} 核 · 内存 {ram_gb:.1f} GB · "
        f"可用磁盘 {free_disk_gb:.1f} GB · {gpu_label} · Ollama {ollama_label}"
    )

    return {
        "platform": sys_name,
        "arch": arch,
        "is_apple_silicon": is_apple_silicon,
        "cpu_cores": cores,
        "gpus": gpus,
        "max_vram_mb": max_vram_mb,
        "ram_gb": ram_gb,
        "free_disk_gb": free_disk_gb,
        "uv_available": bool(uv_bin),
        "ollama": ollama_info,
        "summary": summary,
    }


def meets_nominal_ram(ram_gb: float, min_ram_gb: float) -> bool:
    """Allow ~12.5% OS/iGPU hardware-reserved RAM tolerance (e.g. 16 GB PC reporting 15.5 GB)."""
    if min_ram_gb <= 0:
        return True
    return float(ram_gb) >= float(min_ram_gb) * 0.875


def evaluate_catalog(hw: dict[str, Any]) -> tuple[str, str, dict[str, dict[str, Any]]]:
    """Rank and annotate every offline OCR model against host hardware capabilities."""
    ram_gb = float(hw.get("ram_gb", 0.0))
    free_disk_gb = float(hw.get("free_disk_gb", 0.0))
    max_vram_mb = int(hw.get("max_vram_mb", 0))
    gpus = hw.get("gpus") or []
    uv_ok = bool(hw.get("uv_available"))
    ollama = hw.get("ollama") or {}
    ollama_running = bool(ollama.get("running"))

    # Select best 100% offline recommendation for this host
    if len(gpus) > 0 and max_vram_mb >= 8192 and meets_nominal_ram(ram_gb, 16.0) and free_disk_gb >= 12.0 and uv_ok:
        rec_id = "deepseek_ocr2_local"
        rec_text = (
            f"检测到独立显卡 {gpus[0]['name']} ({max_vram_mb} MB)，推荐下载「DeepSeek-OCR 2 旗舰 CUDA 本地包」，"
            "或安装轻量的「GLM-OCR 0.9B / PP-OCRv5 高精度包」配合涉密离线混合模式使用。"
        )
    elif meets_nominal_ram(ram_gb, 8.0) and free_disk_gb >= 4.0 and ollama_running:
        rec_id = "ollama_glm_ocr"
        rec_text = (
            "检测到本机已运行离线 Ollama 服务且内存充裕，强烈推荐下载「GLM-OCR 0.9B 轻量离线视觉版」，"
            "搭配「PP-OCRv5 高精度包」在涉密严格离线模式下兼顾速度与复杂表格/手写体精度。"
        )
    elif meets_nominal_ram(ram_gb, 8.0) and free_disk_gb >= 0.8:
        rec_id = "ppocrv5_server"
        rec_text = (
            "当前电脑无 CUDA 独显（涉密严格离线），推荐先直接下载零依赖的「PP-OCRv5 高精度离线权重包」(165 MB)；"
            "若需复杂表格/手写更强理解，安装启动开源 Ollama 后即可一键拉取仅 0.9B 的「GLM-OCR」。"
        )
    elif meets_nominal_ram(ram_gb, 4.0) and free_disk_gb >= 0.3:
        rec_id = "ppocrv5_mobile"
        rec_text = (
            f"当前主机内存 {ram_gb:.1f} GB 偏紧凑，推荐下载仅 ~21 MB 的「PP-OCRv5 轻量离线增强包」，纯 CPU 秒级运行且完全离线。"
        )
    else:
        rec_id = "rapidocr_builtin"
        rec_text = "磁盘或内存空间极紧张，推荐直接使用零占用的内置「RapidOCR (PP-OCRv4)」全离线运行。"

    evaluations: dict[str, dict[str, Any]] = {}
    for m in OFFLINE_OCR_CATALOG:
        mid = m["model_id"]
        reasons: list[str] = []
        min_ram = float(m.get("min_ram_gb", 0.0))
        min_disk = float(m.get("min_disk_gb", 0.0))
        min_vram = int(m.get("min_vram_mb", 0))

        if not meets_nominal_ram(ram_gb, min_ram):
            reasons.append(f"内存不足：当前 {ram_gb:.1f} GB，建议 >= {min_ram:.0f} GB")
        if free_disk_gb < min_disk:
            reasons.append(f"磁盘可用空间不足：当前 {free_disk_gb:.1f} GB，需预留 >= {min_disk:.1f} GB")

        if m["runner"] == "deepseek_addon":
            if len(gpus) == 0:
                reasons.append(
                    "macOS（无 NVIDIA CUDA 显卡）无法运行 CUDA 版，建议改用上方的 GLM-OCR 或 PP-OCRv5"
                    if hw.get("platform") == "Darwin"
                    else "未检测到 NVIDIA CUDA 独显（nvidia-smi 不可用）"
                )
            elif max_vram_mb < min_vram:
                reasons.append(f"显存不足：当前 {max_vram_mb} MB，至少需要 {min_vram} MB (8 GB)")
            if not uv_ok:
                reasons.append("未找到 uv 包管理器")

        if reasons:
            fit = "insufficient"
            can_download = False
            action_hint = reasons[0]
        elif m.get("requires_ollama") and not ollama_running:
            fit = "needs_runtime"
            can_download = False
            reasons = ["本机硬件已满足！请先在本机安装并启动免费开源的 Ollama 服务（仅连接 127.0.0.1），刷新体检后即可一键下载"]
            action_hint = "需先在本机启动 Ollama"
        else:
            fit = "recommended" if mid == rec_id else "compatible"
            can_download = True
            action_hint = "最适合当前电脑" if fit == "recommended" else "本机可流畅离线运行"

        evaluations[mid] = {
            "fit": fit,
            "can_download": can_download,
            "reasons": reasons,
            "action_hint": action_hint,
        }

    return rec_id, rec_text, evaluations


def _match_ollama_tag(target_tag: str, installed_models: list[str]) -> bool:
    tl = target_tag.strip().lower()
    if not tl:
        return False
    for name in installed_models:
        nl = name.strip().lower()
        if nl == tl or nl == f"{tl}:latest" or (":" not in tl and nl.split(":", 1)[0] == tl):
            return True
    return False


def _parse_total_from_headers(resp: httpx.Response, offset: int) -> int:
    cr = resp.headers.get("content-range") or resp.headers.get("Content-Range") or ""
    if "/" in cr:
        tail = cr.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    cl = resp.headers.get("content-length") or resp.headers.get("Content-Length") or ""
    if cl.isdigit():
        return offset + int(cl) if resp.status_code == 206 else int(cl)
    return 0


class OfflineOCRHub:
    """Manages multi-tier offline OCR models, hardware recommendations, and resumable downloads."""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        deepseek_addon: DeepSeekAddonManager | None = None,
        events: Any | None = None,
        ollama_base_url: str = "http://127.0.0.1:11434",
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.addons_dir = self.data_dir / "addons"
        self.deepseek_addon = deepseek_addon or DeepSeekAddonManager(self.data_dir, events=events)
        self.events = events
        self.ollama_base_url = ollama_base_url
        self.http_transport = http_transport

        self._lock = threading.Lock()
        self._runtime_states: dict[str, dict[str, Any]] = {}
        self._pause_events: dict[str, threading.Event] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._onnx_engines: dict[str, RapidOCREngine] = {}

    def get_model_dir(self, model_id: str) -> Path:
        if model_id == "deepseek_ocr2_local":
            return self.deepseek_addon.addon_dir
        return self.addons_dir / model_id

    def _has_partial_files(self, model_id: str) -> bool:
        mdir = self.get_model_dir(model_id)
        if not mdir.exists():
            return False
        return any(mdir.glob("*.part"))

    def is_model_installed(self, model_id: str, *, ollama_models: list[str] | None = None) -> bool:
        m = _CATALOG_BY_ID.get(model_id)
        if m is None:
            return False
        runner = m["runner"]
        if runner == "builtin_onnx":
            return True
        if runner == "deepseek_addon":
            return self.deepseek_addon.is_installed()
        if runner == "onnx_download":
            mdir = self.get_model_dir(model_id)
            if not (mdir / "manifest.json").exists():
                return False
            for f in m["files"]:
                fp = mdir / f["filename"]
                if not fp.exists() or fp.stat().st_size <= 0:
                    return False
            return True
        if runner == "ollama_pull":
            if ollama_models is None:
                ollama_models = probe_ollama_local(
                    self.ollama_base_url, http_transport=self.http_transport
                ).get("models", [])
            return _match_ollama_tag(str(m.get("ollama_model", "")), ollama_models)
        return False

    def get_onnx_engine(self, model_id: str) -> RapidOCREngine | None:
        m = _CATALOG_BY_ID.get(model_id)
        if not m or m["runner"] != "onnx_download":
            return None
        if not self.is_model_installed(model_id):
            return None
        with self._lock:
            cached = self._onnx_engines.get(model_id)
            if cached is not None:
                return cached
            mdir = self.get_model_dir(model_id)
            det_file = next(f["filename"] for f in m["files"] if f["role"] == "det")
            rec_file = next(f["filename"] for f in m["files"] if f["role"] == "rec")
            eng = RapidOCREngine(det_model_path=mdir / det_file, rec_model_path=mdir / rec_file)
            self._onnx_engines[model_id] = eng
            return eng

    def _get_or_init_state(self, model_id: str) -> dict[str, Any]:
        with self._lock:
            if model_id not in self._runtime_states:
                self._runtime_states[model_id] = {
                    "state": "not_installed",
                    "percent": 0,
                    "downloaded_mb": 0.0,
                    "total_mb": float((_CATALOG_BY_ID.get(model_id) or {}).get("size_mb", 0.0)),
                    "speed_mb_s": 0.0,
                    "message": "尚未安装（按需下载）",
                }
            return dict(self._runtime_states[model_id])

    def _update_state(self, model_id: str, *, emit: bool = True, **patch: Any) -> None:
        with self._lock:
            cur = self._runtime_states.setdefault(
                model_id,
                {
                    "state": "not_installed",
                    "percent": 0,
                    "downloaded_mb": 0.0,
                    "total_mb": float((_CATALOG_BY_ID.get(model_id) or {}).get("size_mb", 0.0)),
                    "speed_mb_s": 0.0,
                    "message": "",
                },
            )
            cur.update(patch)
            snap = dict(cur)
        if emit and self.events is not None:
            self.events.publish("ocr_hub_progress", {"model_id": model_id, **snap})

    def _build_model_entry(
        self,
        m: dict[str, Any],
        eval_info: dict[str, Any],
        *,
        is_recommended: bool,
        ollama_models: list[str],
    ) -> dict[str, Any]:
        mid = m["model_id"]
        runner = m["runner"]

        if runner == "deepseek_addon":
            ds = self.deepseek_addon.get_status()
            raw_state = ds["install_state"]
            mapped_state = "downloading" if raw_state == "installing" else raw_state
            return {
                **m,
                **eval_info,
                "is_recommended": is_recommended,
                "installed": bool(ds["installed"]),
                "state": "installed" if ds["installed"] else mapped_state,
                "percent": int(ds["percent"]),
                "downloaded_mb": float(ds["disk_usage_mb"]),
                "total_mb": float(m["size_mb"]),
                "speed_mb_s": 0.0,
                "message": str(ds["message"]),
                "disk_usage_mb": float(ds["disk_usage_mb"]),
            }

        installed = self.is_model_installed(mid, ollama_models=ollama_models)
        st = self._get_or_init_state(mid)
        cur_state = st["state"]
        if installed:
            cur_state = "installed"
            st["percent"] = 100
            st["message"] = "已安装就绪，可完全离线使用"
        elif cur_state == "not_installed" and runner == "onnx_download" and self._has_partial_files(mid):
            cur_state = "paused"
            part_bytes = sum(p.stat().st_size for p in self.get_model_dir(mid).glob("*.part"))
            st["downloaded_mb"] = round(part_bytes / float(1024 * 1024), 2)
            st["percent"] = min(99, int(round(100.0 * st["downloaded_mb"] / max(0.1, float(m["size_mb"])))))
            st["message"] = "检测到未完断点，可继续下载"

        disk_usage_mb = 0.0
        mdir = self.get_model_dir(mid)
        if runner == "onnx_download" and mdir.exists():
            disk_usage_mb = round(
                sum(p.stat().st_size for p in mdir.rglob("*") if p.is_file()) / float(1024 * 1024),
                1,
            )

        return {
            **m,
            **eval_info,
            "is_recommended": is_recommended,
            "installed": installed,
            "state": cur_state,
            "percent": int(st["percent"]),
            "downloaded_mb": float(st["downloaded_mb"]),
            "total_mb": float(st["total_mb"]),
            "speed_mb_s": float(st["speed_mb_s"]),
            "message": str(st["message"]),
            "disk_usage_mb": disk_usage_mb,
        }

    def get_catalog_status(
        self,
        *,
        hardware_override: dict[str, Any] | None = None,
        ollama_base_url: str | None = None,
    ) -> dict[str, Any]:
        ob = ollama_base_url or self.ollama_base_url
        hw = hardware_override or inspect_host_hardware(
            self.addons_dir,
            ollama_base_url=ob,
            ollama_probe=lambda: probe_ollama_local(ob, http_transport=self.http_transport),
        )
        rec_id, rec_text, evals = evaluate_catalog(hw)
        ollama_models = list((hw.get("ollama") or {}).get("models") or [])

        models_out = [
            self._build_model_entry(
                m,
                evals[m["model_id"]],
                is_recommended=(m["model_id"] == rec_id),
                ollama_models=ollama_models,
            )
            for m in OFFLINE_OCR_CATALOG
        ]
        return {
            "strict_offline_default": True,
            "recommended_model_id": rec_id,
            "recommendation": rec_text,
            "hardware": hw,
            "models": models_out,
        }

    def join_worker(self, model_id: str, timeout: float = 5.0) -> None:
        with self._lock:
            th = self._threads.get(model_id)
        if th is not None and th.is_alive():
            th.join(timeout=timeout)

    def start_download(
        self,
        model_id: str,
        *,
        use_cn_mirror: bool = True,
        force: bool = False,
        hardware_override: dict[str, Any] | None = None,
        ollama_base_url: str | None = None,
    ) -> dict[str, Any]:
        m = _CATALOG_BY_ID.get(model_id)
        if m is None:
            raise KeyError(f"未知离线 OCR 模型: {model_id}")

        runner = m["runner"]
        if runner == "builtin_onnx":
            return self.get_catalog_status(hardware_override=hardware_override)

        if runner == "deepseek_addon":
            self.deepseek_addon.start_install(
                use_cn_mirror=use_cn_mirror,
                force=force,
                hardware_override=hardware_override,
            )
            return self.get_catalog_status(hardware_override=hardware_override)

        status_before = self.get_catalog_status(
            hardware_override=hardware_override,
            ollama_base_url=ollama_base_url,
        )
        entry = next(x for x in status_before["models"] if x["model_id"] == model_id)
        if not entry["can_download"] and not force:
            raise ValueError("硬件或本地运行时校验未通过：" + "；".join(entry["reasons"] or [entry["action_hint"]]))
        if entry["requires_ollama"] and not status_before["hardware"]["ollama"]["running"]:
            # Ollama pull literally cannot proceed without Ollama HTTP server even with force unless mocked
            if self.http_transport is None:
                raise ValueError("未检测到本地 Ollama 运行，请先启动本机 Ollama 服务后再点击下载")

        with self._lock:
            cur_st = (self._runtime_states.get(model_id) or {}).get("state")
            if cur_st == "downloading":
                return status_before
            pause_ev = self._pause_events.setdefault(model_id, threading.Event())
            cancel_ev = self._cancel_events.setdefault(model_id, threading.Event())
            pause_ev.clear()
            cancel_ev.clear()

        if runner == "onnx_download":
            self._update_state(
                model_id,
                state="downloading",
                message="正在连接镜像源并校验断点...",
            )
            th = threading.Thread(
                target=self._worker_download_onnx,
                args=(model_id, use_cn_mirror, pause_ev, cancel_ev),
                daemon=True,
                name=f"ocr-dl-{model_id}",
            )
        else:  # ollama_pull
            ob = ollama_base_url or self.ollama_base_url
            if not is_local_only_url(ob):
                raise ValueError("涉密严格离线模式下仅允许连接本机 Ollama (127.0.0.1 / localhost)")
            self._update_state(
                model_id,
                state="downloading",
                message="正在通知本机 Ollama 拉取离线模型...",
            )
            th = threading.Thread(
                target=self._worker_pull_ollama,
                args=(model_id, ob, pause_ev, cancel_ev),
                daemon=True,
                name=f"ocr-ollama-{model_id}",
            )

        with self._lock:
            self._threads[model_id] = th
        th.start()
        return self.get_catalog_status(hardware_override=hardware_override, ollama_base_url=ollama_base_url)

    def _worker_download_onnx(
        self,
        model_id: str,
        use_cn_mirror: bool,
        pause_ev: threading.Event,
        cancel_ev: threading.Event,
    ) -> None:
        m = _CATALOG_BY_ID[model_id]
        mdir = self.get_model_dir(model_id)
        mdir.mkdir(parents=True, exist_ok=True)
        files: list[dict[str, str]] = list(m["files"])
        est_total_bytes = max(1, int(float(m["size_mb"]) * 1024 * 1024))

        try:
            # Calculate already completed files bytes for accurate multi-file resume progress
            completed_bytes = sum(
                (mdir / f["filename"]).stat().st_size
                for f in files
                if (mdir / f["filename"]).exists()
            )
            with httpx.Client(
                transport=self.http_transport,
                follow_redirects=True,
                timeout=httpx.Timeout(connect=12.0, read=60.0, write=30.0, pool=10.0),
            ) as client:
                for idx, f in enumerate(files, start=1):
                    if cancel_ev.is_set() or pause_ev.is_set():
                        break
                    final_path = mdir / f["filename"]
                    part_path = mdir / f"{f['filename']}.part"
                    if final_path.exists() and final_path.stat().st_size > 0:
                        continue

                    offset = part_path.stat().st_size if part_path.exists() else 0
                    urls = [f["url_cn"], f["url_global"]] if use_cn_mirror else [f["url_global"], f["url_cn"]]

                    last_err: Exception | None = None
                    downloaded_this_file = False
                    for url in urls:
                        if cancel_ev.is_set() or pause_ev.is_set():
                            break
                        headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
                        try:
                            with client.stream("GET", url, headers=headers) as resp:
                                if resp.status_code == 416 and part_path.exists() and part_path.stat().st_size > 0:
                                    part_path.replace(final_path)
                                    completed_bytes += final_path.stat().st_size
                                    downloaded_this_file = True
                                    break
                                if resp.status_code not in (200, 206):
                                    raise RuntimeError(f"HTTP {resp.status_code}")

                                append_mode = offset > 0 and resp.status_code == 206
                                cur_offset = offset if append_mode else 0
                                f_total = _parse_total_from_headers(resp, cur_offset)
                                denom_bytes = max(est_total_bytes, completed_bytes + f_total)
                                mode = "ab" if append_mode else "wb"
                                t_last = time.perf_counter()
                                bytes_since_tick = 0
                                written_cur = cur_offset

                                with part_path.open(mode) as fp:
                                    for chunk in resp.iter_bytes(chunk_size=65536):
                                        if cancel_ev.is_set() or pause_ev.is_set():
                                            break
                                        if not chunk:
                                            continue
                                        fp.write(chunk)
                                        written_cur += len(chunk)
                                        bytes_since_tick += len(chunk)
                                        now = time.perf_counter()
                                        if now - t_last >= 0.12 or bytes_since_tick >= 512 * 1024:
                                            dt = max(0.001, now - t_last)
                                            spd = round((bytes_since_tick / float(1024 * 1024)) / dt, 2)
                                            tot_dl = completed_bytes + written_cur
                                            pct = min(99, int(round(100.0 * tot_dl / float(denom_bytes))))
                                            self._update_state(
                                                model_id,
                                                state="downloading",
                                                percent=pct,
                                                downloaded_mb=round(tot_dl / float(1024 * 1024), 2),
                                                total_mb=round(denom_bytes / float(1024 * 1024), 2),
                                                speed_mb_s=spd,
                                                message=f"下载组件 {idx}/{len(files)}：{f['filename']}",
                                            )
                                            t_last = now
                                            bytes_since_tick = 0
                                    fp.flush()

                                if cancel_ev.is_set():
                                    return
                                if pause_ev.is_set():
                                    tot_dl = completed_bytes + (part_path.stat().st_size if part_path.exists() else 0)
                                    pct = min(99, int(round(100.0 * tot_dl / float(max(est_total_bytes, tot_dl + 1)))))
                                    self._update_state(
                                        model_id,
                                        state="paused",
                                        percent=pct,
                                        downloaded_mb=round(tot_dl / float(1024 * 1024), 2),
                                        speed_mb_s=0.0,
                                        message="已暂停下载（保留断点，可随时点击继续下载）",
                                    )
                                    return

                                if not part_path.exists() or part_path.stat().st_size <= 0:
                                    raise RuntimeError("下载文件内容为空")
                                part_path.replace(final_path)
                                completed_bytes += final_path.stat().st_size
                                downloaded_this_file = True
                                break
                        except Exception as exc:
                            last_err = exc

                    if pause_ev.is_set() or cancel_ev.is_set():
                        break
                    if not downloaded_this_file:
                        raise RuntimeError(f"镜像下载失败 ({f['filename']}): {last_err}")

            if cancel_ev.is_set():
                self._cleanup_partial_files(model_id)
                self._update_state(model_id, state="not_installed", percent=0, downloaded_mb=0.0, speed_mb_s=0.0, message="已取消下载")
                return
            if pause_ev.is_set():
                return

            (mdir / "manifest.json").write_text(
                json.dumps(
                    {
                        "model_id": model_id,
                        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "use_cn_mirror": use_cn_mirror,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            with self._lock:
                self._onnx_engines.pop(model_id, None)
            actual_mb = round(
                sum((mdir / f["filename"]).stat().st_size for f in files) / float(1024 * 1024),
                2,
            )
            self._update_state(
                model_id,
                state="installed",
                percent=100,
                downloaded_mb=actual_mb,
                total_mb=actual_mb,
                speed_mb_s=0.0,
                message="下载完成，可立即零联网切换使用",
            )
        except Exception as exc:
            self._update_state(
                model_id,
                state="failed",
                speed_mb_s=0.0,
                message=f"下载失败: {exc}",
            )

    def _worker_pull_ollama(
        self,
        model_id: str,
        ollama_base_url: str,
        pause_ev: threading.Event,
        cancel_ev: threading.Event,
    ) -> None:
        m = _CATALOG_BY_ID[model_id]
        tag = str(m["ollama_model"])
        pull_url = f"{ollama_base_url.rstrip('/')}/api/pull"
        try:
            with httpx.Client(
                transport=self.http_transport,
                timeout=httpx.Timeout(connect=10.0, read=120.0, write=15.0, pool=10.0),
            ) as client:
                with client.stream("POST", pull_url, json={"name": tag, "stream": True}) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(f"Ollama HTTP {resp.status_code}")
                    for raw_line in resp.iter_lines():
                        if cancel_ev.is_set():
                            break
                        if pause_ev.is_set():
                            cur_pct = self._get_or_init_state(model_id)["percent"]
                            self._update_state(
                                model_id,
                                state="paused",
                                percent=cur_pct,
                                speed_mb_s=0.0,
                                message="已暂停 Ollama 模型拉取（Ollama 已缓存已完成分层，可继续）",
                            )
                            return
                        line = (raw_line or "").strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if evt.get("error"):
                            raise RuntimeError(str(evt["error"]))
                        completed = int(evt.get("completed") or 0)
                        total = int(evt.get("total") or 0)
                        status_txt = str(evt.get("status") or "拉取中")
                        if total > 0:
                            pct = min(99, int(round(100.0 * completed / float(total))))
                            self._update_state(
                                model_id,
                                state="downloading",
                                percent=pct,
                                downloaded_mb=round(completed / float(1024 * 1024), 1),
                                total_mb=round(total / float(1024 * 1024), 1),
                                message=f"Ollama: {status_txt}",
                            )
                        else:
                            self._update_state(model_id, state="downloading", message=f"Ollama: {status_txt}")

            if cancel_ev.is_set():
                self._update_state(model_id, state="not_installed", percent=0, downloaded_mb=0.0, message="已取消拉取")
                return
            if pause_ev.is_set():
                return

            self._update_state(
                model_id,
                state="installed",
                percent=100,
                downloaded_mb=float(m["size_mb"]),
                total_mb=float(m["size_mb"]),
                speed_mb_s=0.0,
                message="Ollama 离线模型已拉取完成",
            )
        except Exception as exc:
            self._update_state(
                model_id,
                state="failed",
                speed_mb_s=0.0,
                message=f"Ollama 拉取失败: {exc}",
            )

    def _cleanup_partial_files(self, model_id: str) -> None:
        mdir = self.get_model_dir(model_id)
        if not mdir.exists():
            return
        for p in mdir.glob("*.part"):
            p.unlink(missing_ok=True)
        if not (mdir / "manifest.json").exists():
            shutil.rmtree(mdir, ignore_errors=True)

    def pause_download(self, model_id: str) -> dict[str, Any]:
        m = _CATALOG_BY_ID.get(model_id)
        if m is None:
            raise KeyError(f"未知离线 OCR 模型: {model_id}")
        if m["runner"] == "deepseek_addon":
            self.deepseek_addon.pause_install()
            return self.get_catalog_status()
        with self._lock:
            self._pause_events.setdefault(model_id, threading.Event()).set()
        self.join_worker(model_id, timeout=2.0)
        with self._lock:
            cur = dict(self._runtime_states.get(model_id) or {})
        if cur.get("state") == "downloading" or (cur.get("state") != "installed" and self._has_partial_files(model_id)):
            self._update_state(
                model_id,
                state="paused",
                speed_mb_s=0.0,
                message="已暂停下载（保留断点，可随时点击继续下载）",
            )
        return self.get_catalog_status()

    def cancel_download(self, model_id: str) -> dict[str, Any]:
        m = _CATALOG_BY_ID.get(model_id)
        if m is None:
            raise KeyError(f"未知离线 OCR 模型: {model_id}")
        if m["runner"] == "deepseek_addon":
            self.deepseek_addon.cancel_install()
            return self.get_catalog_status()
        with self._lock:
            self._pause_events.setdefault(model_id, threading.Event()).clear()
            self._cancel_events.setdefault(model_id, threading.Event()).set()
        self.join_worker(model_id, timeout=2.0)
        self._cleanup_partial_files(model_id)
        self._update_state(
            model_id,
            state="not_installed",
            percent=0,
            downloaded_mb=0.0,
            total_mb=float(m["size_mb"]),
            speed_mb_s=0.0,
            message="已取消下载并清理断点缓存",
        )
        return self.get_catalog_status()

    def uninstall_model(self, model_id: str, *, ollama_base_url: str | None = None) -> dict[str, Any]:
        m = _CATALOG_BY_ID.get(model_id)
        if m is None:
            raise KeyError(f"未知离线 OCR 模型: {model_id}")
        runner = m["runner"]
        if runner == "builtin_onnx":
            raise ValueError("内置极速 RapidOCR 不可卸载")
        if runner == "deepseek_addon":
            self.deepseek_addon.uninstall()
            return self.get_catalog_status()

        with self._lock:
            self._cancel_events.setdefault(model_id, threading.Event()).set()
            self._onnx_engines.pop(model_id, None)
        self.join_worker(model_id, timeout=2.0)

        if runner == "onnx_download":
            mdir = self.get_model_dir(model_id)
            if mdir.exists():
                shutil.rmtree(mdir, ignore_errors=True)
        elif runner == "ollama_pull":
            ob = ollama_base_url or self.ollama_base_url
            if is_local_only_url(ob):
                try:
                    with httpx.Client(transport=self.http_transport, timeout=5.0) as client:
                        client.request("DELETE", f"{ob.rstrip('/')}/api/delete", json={"name": m["ollama_model"]})
                except Exception:
                    pass

        self._update_state(
            model_id,
            state="not_installed",
            percent=0,
            downloaded_mb=0.0,
            total_mb=float(m["size_mb"]),
            speed_mb_s=0.0,
            message="已卸载并释放空间",
        )
        return self.get_catalog_status(ollama_base_url=ollama_base_url)
