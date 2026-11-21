"""Tests for DeepSeek-OCR 2 hardware pre-check, isolated add-on manager, SmartOCRRouter, and API."""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from book_cleaner.api import create_app
from book_cleaner.config import AppConfig
from book_cleaner.db import Database
from book_cleaner.pipeline.deepseek_addon import (
    DeepSeekAddonManager,
    inspect_hardware_for_deepseek,
)
from book_cleaner.pipeline.ocr_engine import OCRBlock, OCRResult
from book_cleaner.pipeline.ocr_router import OCRSettings, SmartOCRRouter, recognize_with_deepseek_api


def test_hardware_precheck_matrix(tmp_path):
    # 1. Eligible Windows NVIDIA host
    ok_hw = inspect_hardware_for_deepseek(
        tmp_path,
        gpu_probe=lambda: [{"name": "NVIDIA GeForce RTX 4070", "vram_mb": 12288, "driver_version": "560.0"}],
        disk_probe=lambda _: 50.0,
        ram_probe=lambda: 32.0,
        uv_probe=lambda: "/usr/bin/uv",
        platform_system="Windows",
    )
    assert ok_hw["can_run_local"] is True
    assert ok_hw["status"] == "eligible"
    assert all(ok_hw["checks"].values())

    # 2. No NVIDIA GPU (e.g. integrated graphics / macOS)
    no_gpu = inspect_hardware_for_deepseek(
        tmp_path,
        gpu_probe=lambda: [],
        disk_probe=lambda _: 100.0,
        ram_probe=lambda: 32.0,
        uv_probe=lambda: "/usr/bin/uv",
        platform_system="Darwin",
    )
    assert no_gpu["can_run_local"] is False
    assert no_gpu["status"] == "insufficient_hardware"
    assert no_gpu["checks"]["gpu"] is False
    assert "RapidOCR" in no_gpu["recommendation"]

    # 3. Low VRAM (< 8 GB)
    low_vram = inspect_hardware_for_deepseek(
        tmp_path,
        gpu_probe=lambda: [{"name": "NVIDIA GeForce GTX 1650", "vram_mb": 4096}],
        disk_probe=lambda _: 50.0,
        ram_probe=lambda: 16.0,
        uv_probe=lambda: "/usr/bin/uv",
        platform_system="Windows",
    )
    assert low_vram["can_run_local"] is False
    assert low_vram["checks"]["vram"] is False

    # 4. Insufficient free disk (< 12 GB)
    low_disk = inspect_hardware_for_deepseek(
        tmp_path,
        gpu_probe=lambda: [{"name": "NVIDIA RTX 3080", "vram_mb": 10240}],
        disk_probe=lambda _: 3.2,
        ram_probe=lambda: 32.0,
        uv_probe=lambda: "/usr/bin/uv",
        platform_system="Windows",
    )
    assert low_disk["can_run_local"] is False
    assert low_disk["checks"]["disk"] is False


def test_addon_manager_blocks_incompatible_install_and_uninstalls(tmp_path):
    mgr = DeepSeekAddonManager(tmp_path)
    bad_hw = inspect_hardware_for_deepseek(
        tmp_path,
        gpu_probe=lambda: [],
        disk_probe=lambda _: 50.0,
        ram_probe=lambda: 16.0,
        uv_probe=lambda: "/usr/bin/uv",
        platform_system="Windows",
    )
    with pytest.raises(ValueError, match="硬件检测未通过"):
        mgr.start_install(force=False, hardware_override=bad_hw)
    assert mgr.is_installed() is False

    # Simulate partial directory and verify uninstall cleans it up
    mgr.addon_dir.mkdir(parents=True, exist_ok=True)
    (mgr.addon_dir / "dummy.bin").write_bytes(b"123")
    st = mgr.uninstall()
    assert not mgr.addon_dir.exists()
    assert st["installed"] is False


class _StubRapid:
    def __init__(self, conf: float = 0.94, blocks: list[OCRBlock] | None = None, text: str = "Rapid正文") -> None:
        self.conf = conf
        self.blocks = blocks or [
            OCRBlock(bbox=[[10, 10], [200, 10], [200, 40], [10, 40]], text=text, confidence=conf)
        ]
        self.text = text
        self.calls = 0

    def recognize(self, bgr, *, low_conf_threshold: float = 0.82) -> OCRResult:
        self.calls += 1
        return OCRResult(
            blocks=self.blocks,
            raw_text=self.text,
            formatted_text=self.text,
            avg_confidence=self.conf,
            min_confidence=self.conf,
            flags=["low_ocr_confidence"] if self.conf < low_conf_threshold else [],
        )


class _StubAddon:
    def __init__(self, installed: bool, output_text: str = "## DeepSeek本地识别\n\n| 列A | 列B |\n|---|---|\n| 1 | 2 |") -> None:
        self._installed = installed
        self.output_text = output_text
        self.calls = 0

    def is_installed(self) -> bool:
        return self._installed

    def run_local_inference(self, image_path, timeout_s: float = 90.0) -> str:
        self.calls += 1
        return self.output_text


def test_smart_ocr_router_fallback_local_api_and_hybrid(tmp_path):
    db = Database(tmp_path / "router.db")
    try:
        dummy_img = np.full((120, 200, 3), 255, dtype=np.uint8)

        # 1. deepseek_ocr2_local without add-on installed -> automatic fallback to RapidOCR
        db.update_settings({"ocr": {"engine": "deepseek_ocr2_local"}})
        rapid = _StubRapid(conf=0.91, text="本地回退Rapid文本")
        router = SmartOCRRouter(db=db, addon_manager=_StubAddon(installed=False), rapid_engine=rapid)
        res = router.recognize(dummy_img)
        assert res.formatted_text == "本地回退Rapid文本"
        assert "ocr_fallback_rapidocr" in res.flags

        # 2. deepseek_ocr2_local WITH add-on installed -> runs local DeepSeek add-on
        addon_ok = _StubAddon(installed=True)
        router2 = SmartOCRRouter(db=db, addon_manager=addon_ok, rapid_engine=rapid)
        res2 = router2.recognize(dummy_img)
        assert addon_ok.calls == 1
        assert "DeepSeek本地识别" in res2.formatted_text

        # 3. deepseek_ocr2_api (mocked httpx) & HTTP 500 fallback
        def ok_handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content.decode("utf-8"))
            img_url = body["messages"][0]["content"][1]["image_url"]["url"]
            assert img_url.startswith("data:image/jpeg;base64,")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "```markdown\n| 指标 | 值 |\n|---|---|\n| 温度 | 32 |\n```"}}]},
            )

        with httpx.Client(transport=httpx.MockTransport(ok_handler)) as ok_client:
            db.update_settings({"ocr": {"engine": "deepseek_ocr2_api", "api_base_url": "https://mock.local/v1", "api_key": "sk-1"}})
            router_api = SmartOCRRouter(db=db, addon_manager=None, rapid_engine=rapid, api_client=ok_client)
            res_api = router_api.recognize(dummy_img)
            assert "| 指标 | 值 |" in res_api.formatted_text

        def err_handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="server error")

        with httpx.Client(transport=httpx.MockTransport(err_handler)) as err_client:
            router_err = SmartOCRRouter(db=db, addon_manager=None, rapid_engine=rapid, api_client=err_client)
            res_fb = router_err.recognize(dummy_img)
            assert res_fb.formatted_text == "本地回退Rapid文本"
            assert "ocr_fallback_rapidocr" in res_fb.flags

        # 4. hybrid_smart: high-confidence plain page stays RapidOCR; low-conf or table upgrades to DeepSeek
        db.update_settings({"ocr": {"engine": "hybrid_smart", "hybrid_conf_threshold": 0.85, "hybrid_upgrade_tables": True}})
        addon_h = _StubAddon(installed=True, output_text="DeepSeek升级结果")
        rapid_high = _StubRapid(conf=0.95, text="纯文高分页")
        router_h1 = SmartOCRRouter(db=db, addon_manager=addon_h, rapid_engine=rapid_high)
        assert router_h1.recognize(dummy_img).formatted_text == "纯文高分页"
        assert addon_h.calls == 0

        # Low confidence triggers upgrade
        rapid_low = _StubRapid(conf=0.72, text="模糊页")
        router_h2 = SmartOCRRouter(db=db, addon_manager=addon_h, rapid_engine=rapid_low)
        assert router_h2.recognize(dummy_img).formatted_text == "DeepSeek升级结果"
        assert addon_h.calls == 1

        # Multi-column spatial table triggers upgrade even with high confidence
        tbl_blocks = [
            OCRBlock(bbox=[[20, 50], [100, 50], [100, 80], [20, 80]], text="项目", confidence=0.95),
            OCRBlock(bbox=[[280, 50], [360, 50], [360, 80], [280, 80]], text="数值", confidence=0.95),
            OCRBlock(bbox=[[20, 100], [100, 100], [100, 130], [20, 130]], text="压力", confidence=0.95),
            OCRBlock(bbox=[[280, 100], [360, 100], [360, 130], [280, 130]], text="0.2", confidence=0.95),
        ]
        rapid_tbl = _StubRapid(conf=0.96, blocks=tbl_blocks, text="表格")
        router_h3 = SmartOCRRouter(db=db, addon_manager=addon_h, rapid_engine=rapid_tbl)
        assert router_h3.recognize(dummy_img).formatted_text == "DeepSeek升级结果"
        assert addon_h.calls == 2
    finally:
        db.close()


def test_api_deepseek_addon_endpoints_and_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("book_cleaner.pipeline.deepseek_addon.probe_nvidia_gpus", lambda: [])
    cfg = AppConfig(data_dir=tmp_path / "data")
    app = create_app(cfg, start_workers=False)

    with TestClient(app) as client:
        st = client.get("/api/ocr/addons/deepseek")
        assert st.status_code == 200
        data = st.json()
        assert data["installed"] is False
        assert data["hardware"]["can_run_local"] is False

        chk = client.post("/api/ocr/addons/deepseek/check")
        assert chk.status_code == 200
        assert chk.json()["hardware"]["status"] == "insufficient_hardware"

        # Without force=True on non-CUDA host, install refuses cleanly with 400
        bad_install = client.post("/api/ocr/addons/deepseek/install", json={"force": False})
        assert bad_install.status_code == 400
        assert "硬件检测未通过" in bad_install.json()["detail"]

        # Persist OCR engine preference
        saved = client.put(
            "/api/settings",
            json={"ocr": {"engine": "hybrid_smart", "hybrid_conf_threshold": 0.88, "api_model": "deepseek-ai/DeepSeek-OCR-2"}},
        )
        assert saved.status_code == 200
        assert saved.json()["ocr"]["engine"] == "hybrid_smart"
        assert saved.json()["ocr"]["hybrid_conf_threshold"] == 0.88
