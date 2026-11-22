"""Tests for offline Ed25519 licensing, 5-page free gate, 3 commercial packs, and desktop launcher."""

from __future__ import annotations

import base64
import io
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from PIL import Image, ImageDraw

from book_cleaner.api import create_app
from book_cleaner.config import AppConfig
from book_cleaner.db import Database
from book_cleaner.desktop_launcher import (
    build_tray_icon_image,
    check_existing_instance,
    find_available_port,
    resolve_desktop_data_dir,
    save_app_icon,
)
from book_cleaner.licensing import (
    format_machine_code_from_seed,
    generate_keypair,
    get_machine_code,
    is_valid_machine_code,
    issue_license_token,
    normalize_machine_code,
    verify_license_token,
)
from book_cleaner.pipeline.ocr_engine import OCRBlock, OCRResult
from book_cleaner.pipeline.sheet_matcher import match_book_to_template_pack


def _make_distinct_sharp_page(idx: int) -> bytes:
    im = Image.new("RGB", (800, 1100), (248, 248, 246))
    draw = ImageDraw.Draw(im)
    for row in range(12):
        if (row + idx) % 2 == 0 or (idx & (1 << (row % 4))):
            y0 = 80 + row * 78 + (idx * 7) % 19
            x0 = 90 + ((idx * 53 + row * 31) % 240)
            w = 220 + ((idx + row) % 3) * 110
            draw.rectangle([x0, y0, min(720, x0 + w), y0 + 26], fill=(24, 24, 27))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


class _SevenPageStubOCR:
    def __init__(self) -> None:
        self._idx = 0

    def recognize(self, bgr, *, low_conf_threshold: float = 0.82) -> OCRResult:
        self._idx += 1
        n = self._idx
        txt = (
            f"## 批次基础档案 第{n}页\n"
            f"批次号: BATCH-{n:03d}\n"
            f"日期: 2026-11-{10 + n:02d}\n"
            f"投料量: {1000 + n * 10} kg\n"
            f"唯一页标记: PAGE_MARKER_{n:03d}"
        )
        return OCRResult(
            blocks=[OCRBlock(bbox=[[30, 30], [450, 30], [450, 75], [30, 75]], text=txt.splitlines()[0], confidence=0.96)],
            raw_text=txt,
            formatted_text=txt,
            avg_confidence=0.96,
            min_confidence=0.94,
            flags=[],
        )


def test_machine_code_format_and_normalization():
    mc1 = get_machine_code()
    mc2 = get_machine_code()
    assert mc1 == mc2
    assert is_valid_machine_code(mc1)

    derived = format_machine_code_from_seed("custom-board-uuid-123")
    assert is_valid_machine_code(derived)
    assert derived != format_machine_code_from_seed("custom-board-uuid-456")

    raw_no_dash = derived.lower().replace("-", "")
    assert normalize_machine_code(raw_no_dash) == derived
    assert normalize_machine_code(raw_no_dash[2:]) == derived

    with pytest.raises(ValueError):
        normalize_machine_code("BAD-CODE")


def test_ed25519_license_sign_verify_tamper_and_expiry():
    priv_pem, pub_b64 = generate_keypair()
    mc = format_machine_code_from_seed("test-host-001")
    other_mc = format_machine_code_from_seed("test-host-002")

    # 1. Valid perpetual Pro token
    token_pro = issue_license_token(priv_pem, machine_code=mc, tier="pro", note="Order-101")
    st_pro = verify_license_token(token_pro, expected_machine_code=mc, public_key_b64=pub_b64)
    assert st_pro.valid is True
    assert st_pro.active is True
    assert st_pro.tier == "pro"
    assert st_pro.is_perpetual is True
    assert st_pro.export_unlimited is True
    assert st_pro.note == "Order-101"

    # 2. Dated Enterprise token valid before expiry, invalid after expiry
    token_ent = issue_license_token(
        priv_pem,
        machine_code=mc,
        tier="enterprise",
        expires_at="2026-12-31",
        note="Lab-Seat-2",
    )
    st_before = verify_license_token(
        token_ent,
        expected_machine_code=mc,
        public_key_b64=pub_b64,
        now=datetime(2026, 11, 22, tzinfo=timezone.utc),
    )
    assert st_before.valid is True and st_before.active is True and st_before.tier == "enterprise"

    st_after = verify_license_token(
        token_ent,
        expected_machine_code=mc,
        public_key_b64=pub_b64,
        now=datetime(2027, 1, 2, tzinfo=timezone.utc),
    )
    assert st_after.active is False
    assert st_after.tier == "free"
    assert st_after.reason == "expired"

    # 3. Payload tampering (flip pro -> enterprise without re-signing)
    prefix, p_b64, sig_b64 = token_pro.split(".")
    pad = (-len(p_b64)) % 4
    payload_obj = json.loads(base64.urlsafe_b64decode(p_b64 + ("=" * pad)).decode("utf-8"))
    payload_obj["tier"] = "enterprise"
    tampered_p_b64 = base64.urlsafe_b64encode(
        json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")
    tampered_token = f"{prefix}.{tampered_p_b64}.{sig_b64}"
    st_tamper = verify_license_token(tampered_token, expected_machine_code=mc, public_key_b64=pub_b64)
    assert st_tamper.active is False
    assert st_tamper.reason == "invalid_signature"

    # 4. Wrong public key
    _, wrong_pub_b64 = generate_keypair()
    st_wrong_pub = verify_license_token(token_pro, expected_machine_code=mc, public_key_b64=wrong_pub_b64)
    assert st_wrong_pub.active is False
    assert st_wrong_pub.reason == "invalid_signature"

    # 5. Machine mismatch
    st_mismatch = verify_license_token(token_pro, expected_machine_code=other_mc, public_key_b64=pub_b64)
    assert st_mismatch.active is False
    assert st_mismatch.reason == "machine_mismatch"

    # 6. Malformed tokens
    for bad in ("not-a-token", "SS1.onlytwo", "SS2.abc.def", "SS1.!!!.???"):
        st_bad = verify_license_token(bad, expected_machine_code=mc, public_key_b64=pub_b64)
        assert st_bad.active is False
        assert st_bad.reason == "malformed"


def test_issue_license_cli_roundtrip(tmp_path):
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "issue_license.py"
    priv_path = tmp_path / "keys" / "vendor.pem"

    res_kg = subprocess.run(
        [sys.executable, str(script_path), "keygen", "--out-private", str(priv_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    m = re.search(r"Base64URL Public Key:\s*([A-Za-z0-9_-]+)", res_kg.stdout)
    assert m is not None
    pub_b64 = m.group(1)
    assert priv_path.is_file()

    mc = "SS-2345-ABCD-EFGH"
    res_is = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "issue",
            "--machine-code",
            mc,
            "--tier",
            "pro",
            "--note",
            "CLI-Order-99",
            "--private-key",
            str(priv_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    issued_json = json.loads(res_is.stdout)
    token = issued_json["license_key"]
    assert token.startswith("SS1.")

    res_vf = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "verify",
            "--machine-code",
            mc,
            "--public-key-b64",
            pub_b64,
            "--token",
            token,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    vf_json = json.loads(res_vf.stdout)
    assert vf_json["valid"] is True and vf_json["active"] is True


def test_license_api_and_free_5page_export_gate_unlock(tmp_path):
    priv_pem, pub_b64 = generate_keypair()
    cfg = AppConfig(
        data_dir=tmp_path / "data",
        clean_workers=1,
        ocr_workers=1,
        enforce_license_limits=True,
        free_export_page_limit=5,
        license_public_key_b64=pub_b64,
    )
    app = create_app(cfg, start_workers=True, ocr_engine=_SevenPageStubOCR())

    def _wait_ready(client: TestClient, book_id: int, count: int) -> None:
        deadline = time.time() + 15.0
        while time.time() < deadline:
            st = client.get(f"/api/stats?book_id={book_id}").json()["book"]
            if st["completed"] + st["needs_review"] + st["failed"] >= count:
                return
            time.sleep(0.06)
        raise AssertionError("Timed out waiting for 7 pages to settle")

    def _run_export(client: TestClient, book_id: int, fmt: str, template_id: int | None = None):
        payload = {"format": fmt}
        if template_id is not None:
            payload["template_id"] = template_id
        job = client.post(f"/api/books/{book_id}/export", json=payload).json()["job"]
        deadline = time.time() + 10.0
        while time.time() < deadline:
            jd = client.get(f"/api/jobs/{job['id']}").json()
            if jd["status"] in ("DONE", "FAILED"):
                assert jd["status"] == "DONE"
                dl = client.get(f"/api/jobs/{job['id']}/download")
                assert dl.status_code == 200
                return jd, dl.content
            time.sleep(0.05)
        raise AssertionError("Export job timed out")

    with TestClient(app) as client:
        lic0 = client.get("/api/license").json()
        assert lic0["tier"] == "free"
        assert lic0["active"] is False
        assert lic0["free_export_page_limit"] == 5
        assert is_valid_machine_code(lic0["machine_code"])

        # Ingest 7 pages
        book = client.post("/api/books", json={"title": "七页门禁验证集"}).json()
        book_id = book["id"]
        for i in range(7):
            res = client.post(
                f"/api/books/{book_id}/capture",
                files={"file": (f"p_{i + 1}.jpg", _make_distinct_sharp_page(i), "image/jpeg")},
            )
            assert res.status_code == 200
        _wait_ready(client, book_id, 7)

        # Pick multi-sheet pack template
        tpls = client.get("/api/excel-templates").json()["items"]
        pack_tpl = next(t for t in tpls if t["name"] == "多表批次记录与多目标 CatBoost 模板包")

        # While FREE: PDF, Markdown, JSONL, XLSX must all be capped at first 5 pages
        jd_pdf, pdf_bytes = _run_export(client, book_id, "pdf")
        assert jd_pdf["params"]["limited_by_license"] is True
        assert jd_pdf["params"]["original_page_count"] == 7
        assert jd_pdf["params"]["exported_page_count"] == 5
        pdf_doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        assert len(pdf_doc) == 5
        pdf_doc.close()

        jd_md, md_bytes = _run_export(client, book_id, "markdown")
        md_txt = md_bytes.decode("utf-8")
        assert jd_md["params"]["limited_by_license"] is True
        assert md_txt.count("<!-- page:") == 5
        assert "PAGE_MARKER_006" not in md_txt

        jd_jsonl, jsonl_bytes = _run_export(client, book_id, "jsonl")
        lines = [ln for ln in jsonl_bytes.decode("utf-8").splitlines() if ln.strip()]
        assert jd_jsonl["params"]["limited_by_license"] is True
        assert len(lines) == 5

        jd_xlsx, xlsx_bytes = _run_export(client, book_id, "xlsx", template_id=pack_tpl["id"])
        assert jd_xlsx["params"]["limited_by_license"] is True
        wb_free = load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
        try:
            ws_cb = wb_free["CatBoost_特征宽表"]
            # Only batches 1..5 exist, batches 6..7 omitted
            batch_ids = [ws_cb.cell(row=r, column=1).value for r in range(2, ws_cb.max_row + 1)]
            assert "BATCH-005" in batch_ids
            assert "BATCH-006" not in batch_ids
            assert "BATCH-007" not in batch_ids
        finally:
            wb_free.close()

        # Forged license rejected cleanly
        bad_act = client.post("/api/license/activate", json={"license_key": "SS1.Zm9yZ2Vk.Zm9yZ2Vk"})
        assert bad_act.status_code == 400

        # Activate real Ed25519 license for this host
        valid_token = issue_license_token(
            priv_pem,
            machine_code=lic0["machine_code"],
            tier="pro",
            note="Taobao-888",
        )
        ok_act = client.post("/api/license/activate", json={"license_key": valid_token})
        assert ok_act.status_code == 200
        act_data = ok_act.json()
        assert act_data["tier"] == "pro"
        assert act_data["active"] is True
        assert act_data["export_unlimited"] is True

        # Re-export after activation -> ALL 7 pages exported!
        jd_pdf_pro, pdf_pro_bytes = _run_export(client, book_id, "pdf")
        assert jd_pdf_pro["params"]["limited_by_license"] is False
        pdf_pro = pymupdf.open(stream=pdf_pro_bytes, filetype="pdf")
        assert len(pdf_pro) == 7
        pdf_pro.close()

        jd_jsonl_pro, jsonl_pro_bytes = _run_export(client, book_id, "jsonl")
        assert jd_jsonl_pro["params"]["limited_by_license"] is False
        assert len([ln for ln in jsonl_pro_bytes.decode("utf-8").splitlines() if ln.strip()]) == 7

        jd_xlsx_pro, xlsx_pro_bytes = _run_export(client, book_id, "xlsx", template_id=pack_tpl["id"])
        assert jd_xlsx_pro["params"]["limited_by_license"] is False
        wb_pro = load_workbook(io.BytesIO(xlsx_pro_bytes), data_only=True)
        try:
            ws_cb_pro = wb_pro["CatBoost_特征宽表"]
            batch_ids_pro = [ws_cb_pro.cell(row=r, column=1).value for r in range(2, ws_cb_pro.max_row + 1)]
            assert "BATCH-006" in batch_ids_pro
            assert "BATCH-007" in batch_ids_pro
        finally:
            wb_pro.close()

        # Deactivate returns cleanly to free mode
        deact = client.delete("/api/license")
        assert deact.status_code == 200
        assert deact.json()["tier"] == "free" and deact.json()["active"] is False


def test_three_commercial_template_packs_seeded_and_matching(tmp_path):
    db = Database(tmp_path / "packs.db")
    try:
        templates = db.list_excel_templates()
        by_name = {t["name"]: t for t in templates}
        assert "通用书籍混合清洗模板" in by_name
        assert "批次记录与 CatBoost 建模标准模板" in by_name
        assert "多表批次记录与多目标 CatBoost 模板包" in by_name
        assert "实验室检测与研发实验本模板包" in by_name
        assert "财务凭证与合同台账模板包" in by_name

        # 1. Lab notebook routing & multi-y extraction
        lab_tpl = by_name["实验室检测与研发实验本模板包"]
        lab_pages = [
            {
                "id": 1,
                "seq_no": 1,
                "ocr_confidence": 0.95,
                "qa_flags": [],
                "clean_text": "## 样品登记\n实验编号: EXP-2026-01\n实验日期: 2026-11-20\n样品名称: 目标晶体A\n称样量: 15.2 mg",
            },
            {
                "id": 2,
                "seq_no": 2,
                "ocr_confidence": 0.94,
                "qa_flags": [],
                "clean_text": "## 实验条件\n实验编号: EXP-2026-01\n| 采样时刻 | 反应温度 | 体系pH | 流速 |\n| --- | --- | --- | --- |\n| 02h | 45.0℃ | 6.8 | 1.2 |",
            },
            {
                "id": 3,
                "seq_no": 3,
                "ocr_confidence": 0.96,
                "qa_flags": [],
                "clean_text": "## 检测结果\n实验编号: EXP-2026-01\ny_主成分纯度: 98.6%\ny_实验收率: 87.4%",
            },
        ]
        lab_match = match_book_to_template_pack({"id": 1, "title": "研发实验本"}, lab_pages, lab_tpl)
        assert lab_match["coverage"]["matched_pages"] == 3
        assert "y_主成分纯度" in lab_match["catboost_dataset"]["target_y_cols"]
        assert lab_match["catboost_dataset"]["rows"][0]["batch_id"] == "EXP-2026-01"

        # 2. Finance & Contract routing
        fin_tpl = by_name["财务凭证与合同台账模板包"]
        fin_pages = [
            {
                "id": 11,
                "seq_no": 1,
                "ocr_confidence": 0.96,
                "qa_flags": [],
                "clean_text": "## 合同摘要\n合同编号: HT-2026-09\n签订日期: 2026-11-18\n交易对方: 华兴精密仪器\n合同总金额: 128000",
            },
            {
                "id": 12,
                "seq_no": 2,
                "ocr_confidence": 0.95,
                "qa_flags": [],
                "clean_text": "## 费用明细表\n合同编号: HT-2026-09\n| 费用科目 | 数量 | 单价 | 明细金额 |\n| --- | --- | --- | --- |\n| 光学镜头 | 2 | 4000 | 8000 |",
            },
        ]
        fin_match = match_book_to_template_pack({"id": 2, "title": "合同台账"}, fin_pages, fin_tpl)
        assert fin_match["coverage"]["matched_pages"] == 2
        contract_sheet = next(s for s in fin_match["sheets"] if s["name"] == "合同摘要")
        assert len(contract_sheet["rows"]) == 1
        assert contract_sheet["rows"][0]["batch_id"] == "HT-2026-09"
    finally:
        db.close()


def test_desktop_launcher_helpers(tmp_path):
    port = find_available_port(preferred=18765, max_tries=20)
    assert 18765 <= port < 18785
    assert check_existing_instance(port, timeout=0.1) is False

    custom_dir = resolve_desktop_data_dir(tmp_path / "my_desk_data")
    assert custom_dir == (tmp_path / "my_desk_data").resolve()

    img = build_tray_icon_image(64)
    assert img.size == (64, 64)

    ico_path = save_app_icon(tmp_path / "icons" / "app.ico")
    assert ico_path.is_file() and ico_path.stat().st_size > 200
