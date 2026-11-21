"""Tests for hands-on-page zone-aware auto-trigger, auto-HTTPS cert, Windows NIC/path fixes, and Android ADB status."""

from __future__ import annotations

import ipaddress
import time

import cv2
import numpy as np
from cryptography import x509
from fastapi.testclient import TestClient

from book_cleaner.api import _score_lan_candidate, create_app
from book_cleaner.camera import AutoTriggerState
from book_cleaner.config import AppConfig
from book_cleaner.demo_generator import generate_demo_pages
from book_cleaner.pipeline.ocr_engine import OCRBlock, OCRResult
from book_cleaner.tls import ensure_lan_self_signed_cert


def _make_text_page(seed_text: str, *, bg=(242, 228, 196)) -> np.ndarray:
    """Create a realistic 400x300 BGR page (aged yellow by default) with dark central text."""
    img = np.full((300, 400, 3), bg, dtype=np.uint8)
    for row, y in enumerate(range(50, 250, 28)):
        w_line = 120 + ((hash(seed_text + str(row)) & 0x7F) % 100)
        cv2.putText(
            img,
            f"{seed_text} L{row}",
            (95, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (25, 25, 25),
            2,
        )
        cv2.line(img, (95, y + 6), (min(310, 95 + w_line), y + 6), (40, 40, 40), 1)
    return img


def test_hands_on_margin_ignored_and_center_hand_blocks():
    auto = AutoTriggerState()
    auto.config.enabled = True
    auto.config.hands_on_assist = True
    auto.config.margin_ratio = 0.16
    auto.config.settle_ms = 350
    auto.config.cooldown_ms = 500

    page1 = _make_text_page("PageOne")

    # Initial frame on yellowed paper must NOT false-trigger HAND_IN_CENTER
    assert auto.update(page1, now=0.0) is False
    assert auto.update(page1, now=0.1) is False
    assert auto.state == "IDLE"

    # 1. Thumbs move & press inside outer 16% left/right margins ONLY -> stays IDLE!
    page1_thumbs = page1.copy()
    cv2.rectangle(page1_thumbs, (0, 100), (50, 200), (124, 158, 208), -1)  # left margin skin thumb
    cv2.rectangle(page1_thumbs, (350, 110), (399, 210), (124, 158, 208), -1)  # right margin skin thumb
    assert auto.update(page1_thumbs, now=0.2) is False
    assert auto.state == "IDLE"

    # 2. Turning hand enters central 68% text region -> blocks in HAND_IN_CENTER!
    turning = page1_thumbs.copy()
    cv2.rectangle(turning, (120, 70), (280, 230), (124, 158, 208), -1)  # BGR skin tone
    assert auto.update(turning, now=0.3) is False
    assert auto.state == "HAND_IN_CENTER"
    # Even waiting 1 second with hand in center must NOT capture
    assert auto.update(turning, now=1.3) is False
    assert auto.state == "HAND_IN_CENTER"

    # 3. Hand flips to NEW page 2 and retreats back to outer 16% thumbs -> settles and picks sharpest frame!
    page2 = _make_text_page("PageTwoDifferentLayout")
    cv2.rectangle(page2, (0, 100), (50, 200), (124, 158, 208), -1)
    page2_slightly_soft = cv2.GaussianBlur(page2, (3, 3), 0.8)

    # First frame after hand leaves center sees scene change (MOTION_DETECTED)
    assert auto.update(page2_slightly_soft, now=1.4) is False
    # Next steady frame sees low MAD and begins SETTLING
    assert auto.update(page2_slightly_soft, now=1.45) is False
    assert auto.state == "SETTLING"
    # Sharper frame arrives mid-settle
    assert auto.update(page2, now=1.60) is False
    assert auto.state == "SETTLING"
    # Hold stable past settle_ms (1.45 + 0.35 = 1.80) -> triggers!
    assert auto.update(page2, now=1.85) is True
    assert auto.trigger_count == 1
    best = auto.pop_best_frame()
    assert best is not None
    # Verify sharpest frame was chosen over blurred frame
    assert cv2.Laplacian(best, cv2.CV_64F).var() > cv2.Laplacian(page2_slightly_soft, cv2.CV_64F).var()

    # 4. Cooldown finishes, thumb twitches slightly on SAME page 2 -> SAME_PAGE_SKIPPED (no double shoot)
    assert auto.update(page2, now=2.40) is False
    assert auto.state == "IDLE"
    page2_jitter = page2.copy()
    cv2.rectangle(page2_jitter, (180, 140), (220, 160), (235, 220, 185), -1)  # small transient jitter
    # Force motion state then return to exact page 2
    noisy = page2.copy()
    noisy[:, 80:320] = 255
    assert auto.update(noisy, now=2.50) is False
    assert auto.update(page2, now=2.60) is False  # transition frame
    assert auto.update(page2, now=2.65) is False  # steady frame begins SETTLING
    assert auto.state == "SETTLING"
    assert auto.update(page2, now=3.10) is False  # same dHash as captured page2 -> skipped!
    assert auto.state == "IDLE"
    assert auto.trigger_count == 1


def test_self_signed_lan_https_cert_generation_and_reuse(tmp_path):
    cert_dir = tmp_path / "certs"
    c1, k1 = ensure_lan_self_signed_cert(cert_dir, "192.168.1.88")
    assert c1.exists() and k1.exists()

    cert_obj = x509.load_pem_x509_certificate(c1.read_bytes())
    san = cert_obj.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    ips = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    assert "192.168.1.88" in ips
    assert "127.0.0.1" in ips

    mtime_before = c1.stat().st_mtime_ns
    c2, _ = ensure_lan_self_signed_cert(cert_dir, "192.168.1.88")
    assert c2.stat().st_mtime_ns == mtime_before

    # Changing LAN IP regenerates cert with new SAN
    c3, _ = ensure_lan_self_signed_cert(cert_dir, "192.168.31.105")
    cert_obj2 = x509.load_pem_x509_certificate(c3.read_bytes())
    san2 = cert_obj2.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    ips2 = [str(ip) for ip in san2.get_values_for_type(x509.IPAddress)]
    assert "192.168.31.105" in ips2


def test_windows_virtual_nic_filtering():
    assert _score_lan_candidate("vEthernet (WSL (Hyper-V firewall))", "172.25.16.1") is None
    assert _score_lan_candidate("VMware Network Adapter VMnet8", "192.168.152.1") is None
    assert _score_lan_candidate("Mihomo", "198.18.0.1") is None
    assert _score_lan_candidate("Tailscale", "100.64.0.12") is None
    assert _score_lan_candidate("WLAN", "192.168.1.50") > _score_lan_candidate("Adapter2", "10.10.0.2")
    assert _score_lan_candidate("以太网", "192.168.1.51") >= 65


class _StubOCR:
    def recognize(self, bgr, *, low_conf_threshold: float = 0.82) -> OCRResult:
        return OCRResult(
            blocks=[OCRBlock(bbox=[[10, 10], [200, 10], [200, 40], [10, 40]], text="中文路径测试", confidence=0.95)],
            raw_text="中文路径测试",
            formatted_text="中文路径测试",
            avg_confidence=0.95,
            min_confidence=0.95,
            flags=[],
        )


def test_cjk_data_dir_and_android_endpoints(tmp_path):
    # Simulates Windows CJK user profile directory (e.g., C:\Users\张伟\书籍清洗)
    cjk_dir = tmp_path / "张伟的书籍项目" / "data_测试"
    cfg = AppConfig(data_dir=cjk_dir, clean_workers=1, ocr_workers=1)
    app = create_app(cfg, start_workers=True, ocr_engine=_StubOCR())
    samples = dict(generate_demo_pages())

    with TestClient(app) as client:
        adb_st = client.get("/api/android/adb-status")
        assert adb_st.status_code == 200
        body = adb_st.json()
        assert "adb_available" in body
        assert "usb_url" in body

        m_info = client.get("/api/mobile/info?book_id=1").json()
        assert "usb_url" in m_info
        assert "is_https" in m_info

        mobile_html = client.get("/mobile").text
        assert "mobileZeroStoragePill" in mobile_html
        assert "mobileCaptureModeBadge" in mobile_html
        assert "mobileNativeCard" in mobile_html

        mobile_js = client.get("/static/mobile.js").text
        assert "ImageCapture" in mobile_js
        assert "takePhoto" in mobile_js

        book = client.post("/api/books", json={"title": "中文目录测试"}).json()
        cap = client.post(
            f"/api/books/{book['id']}/capture",
            files={"file": ("p1.jpg", samples["04_tilted_clean_page.jpg"], "image/jpeg")},
        )
        assert cap.status_code == 200

        deadline = time.time() + 10.0
        while time.time() < deadline:
            st = client.get(f"/api/stats?book_id={book['id']}").json()["book"]
            if st["ready"] == 1:
                break
            time.sleep(0.08)
        assert client.get(f"/api/stats?book_id={book['id']}").json()["book"]["ready"] == 1
