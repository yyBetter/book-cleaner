"""Tests for L0 QA, L1 OpenCV cleaning, L2 offline RapidOCR, L3 LLM gating, and auto-trigger."""

from __future__ import annotations

import json

import cv2
import httpx
import numpy as np

from book_cleaner.camera import AutoTriggerState
from book_cleaner.demo_generator import generate_demo_pages
from book_cleaner.pipeline.image_clean import CleanProfile, clean_page_image, split_double_spread
from book_cleaner.pipeline.llm_clean import LLMConfig, clean_text_with_llm, should_run_llm
from book_cleaner.pipeline.data_clean import CleaningRules, extract_tables_and_body
from book_cleaner.pipeline.ocr_engine import OCRBlock, RapidOCREngine, format_ocr_blocks
from book_cleaner.pipeline.qa import evaluate_capture_qa
from book_cleaner.storage import decode_image_with_exif


def test_qa_blur_and_duplicate_detection():
    samples = dict(generate_demo_pages())
    tilted_clean = decode_image_with_exif(samples["04_tilted_clean_page.jpg"])
    blurry_shot = decode_image_with_exif(samples["05_blurry_test_shot.jpg"])
    dup_shot = decode_image_with_exif(samples["06_duplicate_test_shot.jpg"])

    qa_clean = evaluate_capture_qa(tilted_clean, recent_dhashes=[])
    assert qa_clean.is_blurry is False
    assert qa_clean.blur_score > 100.0

    qa_blur = evaluate_capture_qa(blurry_shot, recent_dhashes=[qa_clean.dhash])
    assert qa_blur.is_blurry is True
    assert "blurry" in qa_blur.flags

    qa_dup = evaluate_capture_qa(dup_shot, recent_dhashes=[qa_clean.dhash])
    assert qa_dup.is_duplicate is True
    assert "duplicate_page" in qa_dup.flags


def test_image_cleaning_spread_finger_and_shadow_uniformity():
    samples = dict(generate_demo_pages())

    # 1. Double spread splitting (1 -> 2)
    spread = decode_image_with_exif(samples["03_double_spread.jpg"])
    pieces = split_double_spread(spread, split_spread=True)
    assert len(pieces) == 2
    assert [side for side, _ in pieces] == ["L", "R"]
    assert pieces[0][1].shape[1] > 500 and pieces[1][1].shape[1] > 500

    # 2. Yellowed shadow + perspective crop & uniformity improvement
    p1 = decode_image_with_exif(samples["01_yellowed_shadow_tilt.jpg"])
    res1 = clean_page_image(p1, CleanProfile(enhance_mode="clean_color"))
    assert res1.diagnostics["cropped"] is True
    assert res1.diagnostics["bg_uniformity_after"] < res1.diagnostics["bg_uniformity_before"]

    # 3. Curved page + finger removal
    p2 = decode_image_with_exif(samples["02_curved_with_finger.jpg"])
    res2 = clean_page_image(p2, CleanProfile(enhance_mode="bw_sauvola", remove_fingers=True, dewarp=True))
    assert res2.diagnostics["fingers_removed"] >= 1
    assert res2.diagnostics["dewarp_px"] >= 2.5

    # 4. Grayscale crisp mode
    res_gray = clean_page_image(p1, CleanProfile(enhance_mode="grayscale_crisp"))
    assert res_gray.image.ndim == 3 and res_gray.image.shape[0] > 400


def test_offline_rapidocr_on_cleaned_demo_page():
    samples = dict(generate_demo_pages())
    p1 = decode_image_with_exif(samples["01_yellowed_shadow_tilt.jpg"])
    cleaned = clean_page_image(p1, CleanProfile(enhance_mode="clean_color")).image

    engine = RapidOCREngine()
    ocr_res = engine.recognize(cleaned)

    assert ocr_res.avg_confidence > 0.70
    assert len(ocr_res.blocks) >= 3
    joined = ocr_res.formatted_text
    assert "纸质书籍" in joined or "数字化" in joined
    assert "Book Cleaner" in joined


def test_llm_gating_and_mock_openai_call():
    cfg_off = LLMConfig(mode="off", api_key="sk-test")
    assert should_run_llm(cfg_off, 0.40, "test") is False

    cfg_auto = LLMConfig(mode="auto_low_conf", api_key="sk-test", low_conf_threshold=0.85)
    assert should_run_llm(cfg_auto, 0.92, "正常文本") is False
    assert should_run_llm(cfg_auto, 0.71, "识别不清文本") is True

    cfg_all = LLMConfig(mode="all", api_key="sk-test")
    assert should_run_llm(cfg_all, 0.99, "正常文本") is True

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "gpt-4o-mini"
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"message": {"content": "```markdown\n## 第一章\n\n清洗后正文\n```"}}],
                "usage": {"total_tokens": 42},
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        cleaned_txt, meta = clean_text_with_llm("## 第—章\n清洗后正文", cfg_all, client=client)

    assert cleaned_txt == "## 第一章\n\n清洗后正文"
    assert meta["model"] == "gpt-4o-mini"


def test_auto_page_turn_trigger_state_machine():
    auto = AutoTriggerState()
    auto.config.enabled = True
    auto.config.settle_ms = 400
    auto.config.cooldown_ms = 1000

    blank = np.full((240, 320, 3), 240, dtype=np.uint8)
    hand = blank.copy()
    cv2.rectangle(hand, (40, 40), (280, 200), (40, 40, 40), -1)

    assert auto.update(blank, now=0.0) is False
    assert auto.update(blank, now=0.1) is False
    assert auto.state == "IDLE"

    # Motion starts
    assert auto.update(hand, now=0.2) is False
    assert auto.state == "MOTION_DETECTED"

    # Frame stabilizes
    assert auto.update(hand, now=0.3) is False
    assert auto.state == "SETTLING"
    assert auto.update(hand, now=0.5) is False
    # Held steady past settle_ms (0.3 + 0.4 = 0.7) -> triggers!
    assert auto.update(hand, now=0.75) is True
    assert auto.state == "COOLDOWN"
    assert auto.trigger_count == 1
    # Stays quiet in cooldown
    assert auto.update(hand, now=1.0) is False
    assert auto.update(hand, now=1.85) is False
    assert auto.state == "IDLE"


def test_format_ocr_blocks_reconstructs_markdown_table_and_lists():
    def _box(x0: float, y0: float, x1: float, y1: float, text: str) -> OCRBlock:
        return OCRBlock(
            bbox=[[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
            text=text,
            confidence=0.96,
        )

    blocks = [
        _box(113, 120, 648, 167, "第五章质量门禁与断点续传"),
        _box(109, 200, 977, 237, "数据库采用SQLiteWAL预写日志架构，制定日期：2026年3月15日。"),
        _box(108, 308, 970, 346, "1.清晰度评分低于阈值时立即告警。"),
        _box(108, 421, 970, 457, "2.感知哈希dHash自动捕捉连拍重复页。"),
        # 3-col x 4-row table
        _box(122, 778, 233, 814, "工序阶段"),
        _box(423, 776, 533, 813, "目标指标"),
        _box(713, 776, 822, 810, "备注说明"),
        _box(121, 832, 257, 872, "L0采集质检"),
        _box(423, 830, 580, 869, "清晰度 >= 80"),
        _box(714, 830, 872, 866, "模糊即时蜂鸣"),
        _box(122, 889, 256, 926, "L1图像清洗"),
        _box(423, 887, 577, 924, "阴影波动<5"),
        _box(713, 885, 873, 922, "保留彩图印章"),
        _box(121, 945, 257, 984, "L2离线识别"),
        _box(422, 942, 600, 981, "置信度>=82%"),
        _box(713, 942, 873, 979, "低分自动标黄"),
    ]

    md = format_ocr_blocks(blocks, img_height=1500, img_width=1100)
    assert "## 第五章质量门禁与断点续传" in md
    assert "| 工序阶段 | 目标指标 | 备注说明 |" in md
    assert "| --- | --- | --- |" in md
    assert "| L0采集质检 | 清晰度 >= 80 | 模糊即时蜂鸣 |" in md
    assert "| L2离线识别 | 置信度>=82% | 低分自动标黄 |" in md

    # Feeding standard GFM Markdown + spatial blocks must deduplicate to exactly 1 table
    page_dict = {
        "clean_text": md,
        "ocr_text": md,
        "ocr_blocks": [b.to_dict() for b in blocks],
    }
    tables, body = extract_tables_and_body(page_dict, CleaningRules(), set())
    assert len(tables) == 1
    assert tables[0]["headers"] == ["工序阶段", "目标指标", "备注说明"]
    assert len(tables[0]["rows"]) == 3
    assert "第五章质量门禁与断点续传" in body
    assert "1.清晰度评分低于阈值时立即告警。" in body
    assert "2.感知哈希dHash自动捕捉连拍重复页。" in body


def test_auto_orientation_90_180_270_and_manual_rotate_api(tmp_path):
    from fastapi.testclient import TestClient

    from book_cleaner.api import create_app
    from book_cleaner.config import AppConfig

    samples = dict(generate_demo_pages())
    upright = decode_image_with_exif(samples["04_tilted_clean_page.jpg"])
    engine = RapidOCREngine()

    cases = [
        (cv2.ROTATE_90_CLOCKWISE, 270),
        (cv2.ROTATE_180, 180),
        (cv2.ROTATE_90_COUNTERCLOCKWISE, 90),
    ]
    for rot_code, expected_fix_deg in cases:
        sideways = cv2.rotate(upright, rot_code)
        res = clean_page_image(sideways, CleanProfile(auto_rotate=True, enhance_mode="clean_color"))
        assert res.diagnostics["rotated_deg"] == expected_fix_deg
        assert res.diagnostics["rotation_source"] == "auto"
        assert res.image.shape[0] > res.image.shape[1]
        if expected_fix_deg == 270:
            ocr_res = engine.recognize(res.image)
            assert "质量门禁" in ocr_res.formatted_text or "工序阶段" in ocr_res.formatted_text
            assert "|" in ocr_res.formatted_text
            assert "---" in ocr_res.formatted_text

    # Explicit manual override skips auto-detection and forces exact clockwise rotation
    manual_res = clean_page_image(
        upright,
        CleanProfile(auto_rotate=False, manual_rotate_deg=90),
    )
    assert manual_res.diagnostics["rotated_deg"] == 90
    assert manual_res.diagnostics["rotation_source"] == "manual"
    assert manual_res.image.shape[1] > manual_res.image.shape[0]

    # FastAPI POST /api/pages/{id}/rotate endpoint rotates +90 then -90 synchronously with fresh OCR
    cfg = AppConfig(data_dir=tmp_path / "rot_api_data", clean_workers=1, ocr_workers=1)
    app = create_app(cfg, start_workers=False, ocr_engine=engine)
    with TestClient(app) as client:
        book_id = client.post("/api/books", json={"title": "旋转测试册"}).json()["id"]
        sideways_90 = cv2.rotate(upright, cv2.ROTATE_90_CLOCKWISE)
        ok, enc = cv2.imencode(".jpg", sideways_90)
        assert ok
        cap = client.post(
            f"/api/books/{book_id}/capture",
            files={"file": ("sideways.jpg", enc.tobytes(), "image/jpeg")},
            data={"auto_rotate": "false", "deskew": "false"},
        )
        assert cap.status_code == 200
        page_id = cap.json()["pages"][0]["id"]
        client.app.state.coordinator.process_clean_page(client.get(f"/api/pages/{page_id}").json())

        # Step 1: Rotate +90 -> manual 90
        r1 = client.post(f"/api/pages/{page_id}/rotate", json={"delta_deg": 90, "rerun_ocr": True})
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["clean_meta"]["rotated_deg"] == 90
        assert d1["clean_meta"]["rotation_source"] == "manual"

        # Step 2: Rotate +180 more -> total 270 (restores 90_cw input upright!) & refreshes OCR text
        r2 = client.post(f"/api/pages/{page_id}/rotate", json={"delta_deg": 180, "rerun_ocr": True})
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["clean_meta"]["rotated_deg"] == 270
        assert "质量门禁" in d2["clean_text"] or "工序阶段" in d2["clean_text"]


