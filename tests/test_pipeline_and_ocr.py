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
from book_cleaner.pipeline.ocr_engine import RapidOCREngine
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
