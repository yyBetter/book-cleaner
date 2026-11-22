"""Tests for strict-offline confidentiality mode, host hardware recommendation, and pause/resume OCR downloads."""

from __future__ import annotations

import json
import threading
import time

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from book_cleaner.api import create_app
from book_cleaner.config import AppConfig
from book_cleaner.db import DEFAULT_SETTINGS, Database
from book_cleaner.pipeline.deepseek_addon import DeepSeekAddonManager
from book_cleaner.pipeline.ocr_engine import OCRBlock, OCRResult, RapidOCREngine
from book_cleaner.pipeline.ocr_router import OCRSettings, SmartOCRRouter
from book_cleaner.pipeline.offline_ocr_hub import (
    OfflineOCRHub,
    evaluate_catalog,
    inspect_host_hardware,
    is_local_only_url,
    probe_ollama_local,
)
from book_cleaner.storage import ShardedStorage
from book_cleaner.worker import PipelineCoordinator


def test_host_hardware_matrix_and_recommendations(tmp_path):
    assert is_local_only_url("http://127.0.0.1:11434") is True
    assert is_local_only_url("http://localhost:8080/v1") is True
    assert is_local_only_url("https://api.siliconflow.cn/v1") is False
    assert is_local_only_url("http://192.168.1.20:11434/v1") is False

    # 1. Apple Silicon + Ollama running -> recommends GLM-OCR 0.9B
    hw_mac_ollama = inspect_host_hardware(
        tmp_path,
        gpu_probe=lambda: [],
        disk_probe=lambda _: 40.0,
        ram_probe=lambda: 16.0,
        uv_probe=lambda: "/opt/homebrew/bin/uv",
        ollama_probe=lambda: {"installed": True, "running": True, "base_url": "http://127.0.0.1:11434", "models": []},
        platform_system="Darwin",
        machine_arch="arm64",
        cpu_count=8,
    )
    rec1, _, eval1 = evaluate_catalog(hw_mac_ollama)
    assert rec1 == "ollama_glm_ocr"
    assert eval1["ollama_glm_ocr"]["fit"] == "recommended"
    assert eval1["ppocrv5_server"]["fit"] == "compatible"
    assert eval1["deepseek_ocr2_local"]["fit"] == "insufficient"

    # 2. Apple Silicon WITHOUT Ollama -> recommends zero-dependency PP-OCRv5 Server; marks GLM-OCR needs_runtime
    hw_mac_plain = inspect_host_hardware(
        tmp_path,
        gpu_probe=lambda: [],
        disk_probe=lambda _: 40.0,
        ram_probe=lambda: 16.0,
        uv_probe=lambda: "/opt/homebrew/bin/uv",
        ollama_probe=lambda: {"installed": False, "running": False, "base_url": "http://127.0.0.1:11434", "models": []},
        platform_system="Darwin",
        machine_arch="arm64",
        cpu_count=8,
    )
    rec2, _, eval2 = evaluate_catalog(hw_mac_plain)
    assert rec2 == "ppocrv5_server"
    assert eval2["ollama_glm_ocr"]["fit"] == "needs_runtime"
    assert eval2["ollama_glm_ocr"]["can_download"] is False

    # 3. Windows RTX 4070 12GB -> recommends DeepSeek-OCR 2 CUDA
    hw_cuda = inspect_host_hardware(
        tmp_path,
        gpu_probe=lambda: [{"name": "NVIDIA RTX 4070", "vram_mb": 12288}],
        disk_probe=lambda _: 60.0,
        ram_probe=lambda: 32.0,
        uv_probe=lambda: "uv.exe",
        ollama_probe=lambda: {"installed": False, "running": False, "base_url": "http://127.0.0.1:11434", "models": []},
        platform_system="Windows",
        machine_arch="amd64",
        cpu_count=16,
    )
    rec3, _, _ = evaluate_catalog(hw_cuda)
    assert rec3 == "deepseek_ocr2_local"

    # 4. Low-RAM 4GB Windows office laptop -> recommends ~21MB PP-OCRv5 Mobile
    hw_low_ram = inspect_host_hardware(
        tmp_path,
        gpu_probe=lambda: [],
        disk_probe=lambda _: 10.0,
        ram_probe=lambda: 4.0,
        uv_probe=lambda: "uv.exe",
        ollama_probe=lambda: {"installed": False, "running": False, "base_url": "http://127.0.0.1:11434", "models": []},
        platform_system="Windows",
        machine_arch="amd64",
        cpu_count=4,
    )
    rec4, _, eval4 = evaluate_catalog(hw_low_ram)
    assert rec4 == "ppocrv5_mobile"
    assert eval4["ppocrv5_server"]["fit"] == "insufficient"


def test_onnx_resumable_download_pause_resume_cancel_and_uninstall(tmp_path):
    det_payload = b"DET_HEADER_" + b"D" * (128 * 1024) + b"_DET_END"
    rec_payload = b"REC_HEADER_" + b"R" * (160 * 1024) + b"_REC_END"
    first_chunk_written = threading.Event()
    allow_second_chunk = threading.Event()
    range_headers_seen: list[str] = []

    class _ChunkedRangeStream(httpx.SyncByteStream):
        def __init__(self, chunks: list[bytes], *, gate_after_first: bool = False) -> None:
            self._chunks = chunks
            self._gate = gate_after_first

        def __iter__(self):
            for idx, ch in enumerate(self._chunks):
                yield ch
                if self._gate and idx == 0:
                    first_chunk_written.set()
                    allow_second_chunk.wait(timeout=3.0)

    det_split = len(det_payload) // 2

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        data = det_payload if "det" in url else rec_payload
        rng = req.headers.get("Range") or req.headers.get("range")
        if rng:
            range_headers_seen.append(rng)
            start = int(rng.replace("bytes=", "").split("-")[0])
            slice_bytes = data[start:]
            return httpx.Response(
                206,
                headers={
                    "Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}",
                    "Content-Length": str(len(slice_bytes)),
                },
                stream=_ChunkedRangeStream([slice_bytes]),
            )
        if "det" in url:
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(data))},
                stream=_ChunkedRangeStream([data[:det_split], data[det_split:]], gate_after_first=True),
            )
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(data))},
            stream=_ChunkedRangeStream([data]),
        )

    events: list[dict] = []

    class _Bus:
        def publish(self, event_type, data):
            events.append({"type": event_type, "data": data})

    hw_ok = inspect_host_hardware(
        tmp_path,
        gpu_probe=lambda: [],
        disk_probe=lambda _: 20.0,
        ram_probe=lambda: 8.0,
        uv_probe=lambda: "uv",
        ollama_probe=lambda: {"installed": False, "running": False, "base_url": "http://127.0.0.1:11434", "models": []},
        platform_system="Darwin",
        machine_arch="arm64",
    )
    hub = OfflineOCRHub(tmp_path, events=_Bus(), http_transport=httpx.MockTransport(handler))

    # 1. Start download, wait until first chunk of det is streamed, then PAUSE
    hub.start_download("ppocrv5_mobile", hardware_override=hw_ok)
    assert first_chunk_written.wait(timeout=3.0)
    time.sleep(0.05)  # allow chunk write before setting pause
    paused_status = hub.pause_download("ppocrv5_mobile")
    allow_second_chunk.set()
    hub.join_worker("ppocrv5_mobile", timeout=3.0)

    paused_entry = next(m for m in paused_status["models"] if m["model_id"] == "ppocrv5_mobile")
    assert paused_entry["state"] == "paused"
    part_file = hub.get_model_dir("ppocrv5_mobile") / "ch_PP-OCRv5_det_mobile.onnx.part"
    assert part_file.exists()
    assert 0 < part_file.stat().st_size < len(det_payload)

    # 2. Resume download -> must send HTTP Range header and assemble exact full payloads
    allow_second_chunk.clear()
    hub.start_download("ppocrv5_mobile", hardware_override=hw_ok)
    hub.join_worker("ppocrv5_mobile", timeout=4.0)

    assert any(r.startswith("bytes=") for r in range_headers_seen)
    assert hub.is_model_installed("ppocrv5_mobile") is True
    mdir = hub.get_model_dir("ppocrv5_mobile")
    assert (mdir / "ch_PP-OCRv5_det_mobile.onnx").read_bytes() == det_payload
    assert (mdir / "ch_PP-OCRv5_rec_mobile.onnx").read_bytes() == rec_payload
    eng = hub.get_onnx_engine("ppocrv5_mobile")
    assert isinstance(eng, RapidOCREngine)

    # 3. Uninstall removes installed directory cleanly
    st_after_del = hub.uninstall_model("ppocrv5_mobile")
    del_entry = next(m for m in st_after_del["models"] if m["model_id"] == "ppocrv5_mobile")
    assert del_entry["installed"] is False
    assert not mdir.exists()

    # 4. Partial download followed by explicit CANCEL deletes .part files
    first_chunk_written.clear()
    allow_second_chunk.clear()
    hub.start_download("ppocrv5_mobile", hardware_override=hw_ok)
    assert first_chunk_written.wait(timeout=3.0)
    time.sleep(0.05)
    cancel_st = hub.cancel_download("ppocrv5_mobile")
    allow_second_chunk.set()
    hub.join_worker("ppocrv5_mobile", timeout=3.0)
    cancel_entry = next(m for m in cancel_st["models"] if m["model_id"] == "ppocrv5_mobile")
    assert cancel_entry["state"] == "not_installed"
    assert not part_file.exists()


def test_ollama_streaming_pull_pause_resume_and_uninstall(tmp_path):
    installed_tags: list[str] = []
    pull_gate = threading.Event()
    first_line_emitted = threading.Event()
    pull_calls = 0

    class _NdjsonStream(httpx.SyncByteStream):
        def __init__(self, lines: list[bytes], *, gate_after_first: bool = False) -> None:
            self._lines = lines
            self._gate = gate_after_first

        def __iter__(self):
            for idx, ln in enumerate(self._lines):
                yield ln
                if self._gate and idx == 0:
                    first_line_emitted.set()
                    pull_gate.wait(timeout=3.0)

    def ollama_handler(req: httpx.Request) -> httpx.Response:
        nonlocal pull_calls
        path = req.url.path
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": t} for t in installed_tags]})
        if path == "/api/pull":
            pull_calls += 1
            if pull_calls == 1:
                lines = [
                    (json.dumps({"status": "pulling manifest", "completed": 100, "total": 1000}) + "\n").encode(),
                    (json.dumps({"status": "downloading layer", "completed": 500, "total": 1000}) + "\n").encode(),
                ]
                return httpx.Response(200, stream=_NdjsonStream(lines, gate_after_first=True))
            installed_tags.append("glm-ocr:latest")
            lines = [
                (json.dumps({"status": "verifying", "completed": 1000, "total": 1000}) + "\n").encode(),
                (json.dumps({"status": "success"}) + "\n").encode(),
            ]
            return httpx.Response(200, stream=_NdjsonStream(lines))
        if path == "/api/delete" and req.method == "DELETE":
            installed_tags.clear()
            return httpx.Response(200, json={"status": "deleted"})
        return httpx.Response(404)

    hw_ollama = inspect_host_hardware(
        tmp_path,
        gpu_probe=lambda: [],
        disk_probe=lambda _: 30.0,
        ram_probe=lambda: 16.0,
        uv_probe=lambda: "uv",
        ollama_probe=lambda: {"installed": True, "running": True, "base_url": "http://127.0.0.1:11434", "models": list(installed_tags)},
        platform_system="Darwin",
        machine_arch="arm64",
    )
    hub = OfflineOCRHub(tmp_path, http_transport=httpx.MockTransport(ollama_handler))

    # 1. Start pull -> pause mid-stream
    hub.start_download("ollama_glm_ocr", hardware_override=hw_ollama)
    assert first_line_emitted.wait(timeout=3.0)
    time.sleep(0.05)
    st_pause = hub.pause_download("ollama_glm_ocr")
    pull_gate.set()
    hub.join_worker("ollama_glm_ocr", timeout=3.0)
    p_entry = next(m for m in st_pause["models"] if m["model_id"] == "ollama_glm_ocr")
    assert p_entry["state"] == "paused"

    # 2. Resume pull -> completes and appears in local tags
    hub.start_download("ollama_glm_ocr", hardware_override=hw_ollama)
    hub.join_worker("ollama_glm_ocr", timeout=3.0)
    assert hub.is_model_installed("ollama_glm_ocr", ollama_models=installed_tags) is True

    # 3. Uninstall removes tag via DELETE /api/delete
    hub.uninstall_model("ollama_glm_ocr")
    assert installed_tags == []


def test_deepseek_addon_pause_preserves_partial_dir(tmp_path):
    mgr = DeepSeekAddonManager(tmp_path)
    mgr.addon_dir.mkdir(parents=True, exist_ok=True)
    partial_file = mgr.addon_dir / "weights.bin.partial"
    partial_file.write_bytes(b"partial-data")
    st = mgr.pause_install()
    assert st["install_state"] == "paused"
    assert partial_file.exists()


def test_strict_offline_blocks_external_cloud_and_allows_local_ollama(tmp_path):
    db = Database(tmp_path / "strict_router.db")
    try:
        dummy_img = np.full((120, 200, 3), 255, dtype=np.uint8)
        rapid_stub = type(
            "_Rapid",
            (),
            {
                "recognize": lambda self, bgr, *, low_conf_threshold=0.82: OCRResult(
                    blocks=[OCRBlock(bbox=[[10, 10], [80, 10], [80, 30], [10, 30]], text="本地兜底", confidence=0.92)],
                    raw_text="本地兜底",
                    formatted_text="本地兜底",
                    avg_confidence=0.92,
                    min_confidence=0.92,
                    flags=[],
                )
            },
        )()

        external_calls = 0
        local_ollama_calls = 0

        def router_handler(req: httpx.Request) -> httpx.Response:
            nonlocal external_calls, local_ollama_calls
            if req.url.host not in ("127.0.0.1", "localhost", "::1"):
                external_calls += 1
                return httpx.Response(200, json={"choices": [{"message": {"content": "CLOUD"}}]})
            local_ollama_calls += 1
            return httpx.Response(200, json={"choices": [{"message": {"content": "## 本机GLM-OCR识别\n\n| 参数 | 值 |\n|---|---|\n| 温度 | 32.5 |"}}]})

        with httpx.Client(transport=httpx.MockTransport(router_handler)) as client:
            # 1. strict_offline=True MUST block external cloud API completely
            db.update_settings(
                {
                    "ocr": {
                        "engine": "deepseek_ocr2_api",
                        "strict_offline": True,
                        "api_base_url": "https://api.siliconflow.cn/v1",
                        "api_key": "sk-secret",
                    }
                }
            )
            router = SmartOCRRouter(db=db, addon_manager=None, rapid_engine=rapid_stub, api_client=client)
            res = router.recognize(dummy_img)
            assert external_calls == 0
            assert "ocr_blocked_offline_policy" in res.flags
            assert res.formatted_text == "本地兜底"

            # 2. strict_offline=True ALLOWS loopback 127.0.0.1 Ollama Vision OCR
            db.update_settings(
                {
                    "ocr": {
                        "engine": "ollama_glm_ocr",
                        "strict_offline": True,
                        "ollama_base_url": "http://127.0.0.1:11434",
                    }
                }
            )
            res_local = router.recognize(dummy_img)
            assert local_ollama_calls == 1
            assert external_calls == 0
            assert "本机GLM-OCR识别" in res_local.formatted_text
    finally:
        db.close()


def test_offline_ocr_hub_api_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr("book_cleaner.pipeline.offline_ocr_hub.probe_nvidia_gpus", lambda: [])
    monkeypatch.setattr(
        "book_cleaner.pipeline.offline_ocr_hub.probe_ollama_local",
        lambda base_url="http://127.0.0.1:11434", **kw: {
            "installed": False,
            "running": False,
            "base_url": base_url,
            "models": [],
        },
    )
    cfg = AppConfig(data_dir=tmp_path / "data")
    app = create_app(cfg, start_workers=False)

    with TestClient(app) as client:
        res = client.get("/api/ocr/models")
        assert res.status_code == 200
        body = res.json()
        assert body["strict_offline_default"] is True
        assert len(body["models"]) == 6

        chk = client.post("/api/ocr/models/check")
        assert chk.status_code == 200
        assert "recommended_model_id" in chk.json()

        # Unknown model -> 404
        assert client.post("/api/ocr/models/unknown_ocr/download").status_code == 404
        # CUDA model on non-GPU host -> blocked with 400
        bad_dl = client.post("/api/ocr/models/deepseek_ocr2_local/download", json={"force": False})
        assert bad_dl.status_code == 400
        # Builtin RapidOCR cannot be uninstalled -> 400
        assert client.delete("/api/ocr/models/rapidocr_builtin").status_code == 400

        # Test strict_offline blocks external /api/ocr/test-api and /api/settings/test-llm
        t_ocr = client.post(
            "/api/ocr/test-api",
            json={"api_base_url": "https://api.siliconflow.cn/v1", "api_key": "sk-1", "strict_offline": True},
        )
        assert t_ocr.status_code == 200
        assert t_ocr.json()["ok"] is False
        assert "涉密严格离线模式" in t_ocr.json()["message"]

        t_llm = client.post(
            "/api/settings/test-llm",
            json={"base_url": "https://api.openai.com/v1", "api_key": "sk-1", "strict_offline": True},
        )
        assert t_llm.status_code == 200
        assert t_llm.json()["ok"] is False
        assert "涉密严格离线模式" in t_llm.json()["message"]


def test_windows_igpu_reserved_ram_tolerance(tmp_path):
    # 16 GB Windows PC reporting 15.5 GB usable RAM with Ollama running -> Qwen2.5-VL 3B allowed
    hw_16_running = inspect_host_hardware(
        tmp_path,
        gpu_probe=lambda: [],
        disk_probe=lambda _: 40.0,
        ram_probe=lambda: 15.5,
        uv_probe=lambda: "uv.exe",
        ollama_probe=lambda: {"installed": True, "running": True, "base_url": "http://127.0.0.1:11434", "models": []},
        platform_system="Windows",
        machine_arch="amd64",
        cpu_count=8,
    )
    _, _, eval_16_running = evaluate_catalog(hw_16_running)
    assert eval_16_running["ollama_qwen25vl_3b"]["fit"] == "compatible"
    assert eval_16_running["ollama_qwen25vl_3b"]["can_download"] is True

    # 16 GB Windows PC reporting 15.5 GB usable RAM with Ollama stopped -> needs_runtime (NOT insufficient)
    hw_16_stopped = inspect_host_hardware(
        tmp_path,
        gpu_probe=lambda: [],
        disk_probe=lambda _: 40.0,
        ram_probe=lambda: 15.5,
        uv_probe=lambda: "uv.exe",
        ollama_probe=lambda: {"installed": False, "running": False, "base_url": "http://127.0.0.1:11434", "models": []},
        platform_system="Windows",
        machine_arch="amd64",
        cpu_count=8,
    )
    _, _, eval_16_stopped = evaluate_catalog(hw_16_stopped)
    assert eval_16_stopped["ollama_qwen25vl_3b"]["fit"] == "needs_runtime"

    # 8 GB Windows PC reporting 7.5 GB usable RAM without Ollama -> recommends ppocrv5_server & marks GLM needs_runtime
    hw_8_stopped = inspect_host_hardware(
        tmp_path,
        gpu_probe=lambda: [],
        disk_probe=lambda _: 20.0,
        ram_probe=lambda: 7.5,
        uv_probe=lambda: "uv.exe",
        ollama_probe=lambda: {"installed": False, "running": False, "base_url": "http://127.0.0.1:11434", "models": []},
        platform_system="Windows",
        machine_arch="amd64",
        cpu_count=8,
    )
    rec_8, _, eval_8 = evaluate_catalog(hw_8_stopped)
    assert rec_8 == "ppocrv5_server"
    assert eval_8["ppocrv5_server"]["can_download"] is True
    assert eval_8["ollama_glm_ocr"]["fit"] == "needs_runtime"

    # 12 GB PC (below 14.0 GB tolerance floor) -> still blocked as insufficient for Qwen2.5-VL 3B
    hw_12 = inspect_host_hardware(
        tmp_path,
        gpu_probe=lambda: [],
        disk_probe=lambda _: 40.0,
        ram_probe=lambda: 12.0,
        uv_probe=lambda: "uv.exe",
        ollama_probe=lambda: {"installed": True, "running": True, "base_url": "http://127.0.0.1:11434", "models": []},
        platform_system="Windows",
        machine_arch="amd64",
        cpu_count=8,
    )
    _, _, eval_12 = evaluate_catalog(hw_12)
    assert eval_12["ollama_qwen25vl_3b"]["fit"] == "insufficient"
    assert eval_12["ollama_qwen25vl_3b"]["can_download"] is False


def test_local_ollama_bypasses_env_proxy_and_static_no_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")

    def _handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:11434/api/tags"
        return httpx.Response(200, json={"models": [{"name": "glm-ocr:latest"}]})

    res = probe_ollama_local("http://127.0.0.1:11434", http_transport=httpx.MockTransport(_handler))
    assert res["running"] is True
    assert res["models"] == ["glm-ocr:latest"]

    cfg = AppConfig(data_dir=tmp_path / "cache_test_data")
    app = create_app(cfg, start_workers=False)
    with TestClient(app) as client:
        r_index = client.get("/")
        assert r_index.status_code == 200
        assert r_index.headers.get("cache-control") == "no-cache"

        r_js = client.get("/static/api.js")
        assert r_js.status_code == 200
        assert r_js.headers.get("cache-control") == "no-cache"


def test_ollama_timeout_floor_and_fallback_reason_lifecycle(tmp_path):
    assert float(DEFAULT_SETTINGS["ocr"]["timeout_s"]) == 300.0
    assert OCRSettings().timeout_s == 300.0

    cfg = AppConfig(data_dir=tmp_path / "fallback_data")
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    storage = ShardedStorage(cfg.storage_dir)
    try:
        # Simulate legacy 60s timeout stored in DB + Ollama Qwen2.5-VL 3B selected
        db.update_settings(
            {
                "ocr": {
                    "engine": "ollama_qwen25vl_3b",
                    "strict_offline": True,
                    "ollama_base_url": "http://127.0.0.1:11434",
                    "timeout_s": 60.0,
                }
            }
        )

        observed_timeouts: list[float] = []

        class _TimeoutClient:
            def post(self, url, json=None, headers=None, timeout=None):
                observed_timeouts.append(float(timeout or 0.0))
                raise httpx.ReadTimeout("timed out reading stream")

        class _ConnectErrClient:
            def post(self, url, json=None, headers=None, timeout=None):
                observed_timeouts.append(float(timeout or 0.0))
                raise httpx.ConnectError("connection refused")

        class _OkRapid:
            def recognize(self, bgr, *, low_conf_threshold=0.82):
                blk = OCRBlock(bbox=[[10, 10], [80, 10], [80, 30], [10, 30]], text="保底正文", confidence=0.91)
                return OCRResult(blocks=[blk], raw_text="保底正文", formatted_text="保底正文", avg_confidence=0.91, min_confidence=0.91)

        router = SmartOCRRouter(
            db,
            addon_manager=None,
            rapid_engine=_OkRapid(),  # type: ignore[arg-type]
            api_client=_TimeoutClient(),  # type: ignore[arg-type]
        )
        coord = PipelineCoordinator(cfg, db, storage, ocr_engine=router)

        img = np.full((240, 320, 3), 245, dtype=np.uint8)
        ok, enc = cv2.imencode(".jpg", img)
        assert ok
        book_id = db.ensure_default_book()["id"]
        cap = coord.ingest_image_bytes(book_id=book_id, raw_bytes=enc.tobytes(), source_type="upload", source_ref="qwen.jpg")
        page = cap["pages"][0]
        coord.process_clean_page(page)
        cleaned = db.get_page(page["id"])
        assert cleaned is not None

        # 1. ReadTimeout: timeout_s must enforce >= 300s floor & persist Chinese fallback reason
        after_timeout = coord.process_ocr_page(cleaned)
        assert observed_timeouts and observed_timeouts[-1] >= 300.0
        assert "ocr_fallback_rapidocr" in (after_timeout.get("qa_flags") or [])
        meta1 = after_timeout.get("clean_meta") or {}
        assert meta1.get("ocr_requested_engine") == "ollama_qwen25vl_3b"
        assert meta1.get("ocr_engine_used") == "rapidocr"
        assert "超过 300 秒" in str(meta1.get("ocr_fallback_reason") or "")

        # 2. ConnectError: clear Chinese connection message persisted
        router.api_client = _ConnectErrClient()  # type: ignore[assignment]
        after_conn = coord.process_ocr_page(after_timeout)
        meta2 = after_conn.get("clean_meta") or {}
        assert "无法连接" in str(meta2.get("ocr_fallback_reason") or "")
        assert "127.0.0.1:11434" in str(meta2.get("ocr_fallback_reason") or "")

        # 3. Re-OCR queue (`reclean_page(..., rerun_ocr=True)`) clears stale fallback reason & flags
        requeued = coord.reclean_page(after_conn["id"], profile_patch={}, rerun_ocr=True)
        assert requeued["status"] == "CLEANED"
        assert "ocr_fallback_rapidocr" not in (requeued.get("qa_flags") or [])
        assert "ocr_fallback_reason" not in (requeued.get("clean_meta") or {})
    finally:
        db.close()
