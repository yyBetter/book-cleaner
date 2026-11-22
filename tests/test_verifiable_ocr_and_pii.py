"""Tests for field-level verifiable checks, olmOCR coordinate anchoring & smart VLM routing, and consistent PII redaction."""

from __future__ import annotations

import io
import json
import time

import httpx
import numpy as np
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from PIL import Image, ImageDraw

from book_cleaner.api import create_app
from book_cleaner.config import AppConfig
from book_cleaner.db import Database
from book_cleaner.pipeline.excel_exporter import export_multisheet_pack_workbook
from book_cleaner.pipeline.llm_clean import LLMConfig, extract_sheet_rows_with_llm
from book_cleaner.pipeline.ocr_engine import (
    OCRBlock,
    OCRResult,
    build_coordinate_anchor_text,
)
from book_cleaner.pipeline.ocr_router import (
    OCRSettings,
    SmartOCRRouter,
    evaluate_hybrid_upgrade_triggers,
)
from book_cleaner.pipeline.pii_redactor import ConsistentPIIRedactor
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
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=92)
    return out.getvalue()


def test_verifiable_range_bounds_and_5x_outlier_excel_interception(tmp_path):
    db = Database(tmp_path / "verify.db")
    try:
        tpl = next(t for t in db.list_excel_templates() if t.get("sheets"))
        proc_sheet = next(s for s in tpl["sheets"] if s["sheet_id"] == "sheet_process")
        temp_field = next(f for f in proc_sheet["fields"] if f["key"] == "temp_c")
        assert temp_field["min_val"] == 0.0
        assert temp_field["max_val"] == 100.0

        book = {"id": 1, "title": "过程参数校核册", "isbn": "B-CHECK"}
        pages = [
            {
                "id": 1,
                "seq_no": 1,
                "ocr_confidence": 0.99,  # High OCR confidence yet dropped decimal 31.5 -> 315!
                "qa_flags": [],
                "clean_text": (
                    "## 过程运行记录\n"
                    "批次号: B-ERR\n\n"
                    "| 记录时刻 | 温度 | 溶氧 | 过程指标 |\n"
                    "| --- | --- | --- | --- |\n"
                    "| 04h | 31.5℃ | 45% | 12.0 |\n"
                    "| 08h | 315℃ | 46% | 12.5 |\n"
                    "| 12h | 32.0℃ | 44% | 125.0 |\n"
                ),
            },
            {
                "id": 2,
                "seq_no": 2,
                "ocr_confidence": 0.96,
                "qa_flags": [],
                "clean_text": (
                    "## 过程运行记录\n"
                    "批次号: B-OK\n\n"
                    "| 记录时刻 | 温度 | 溶氧 | 过程指标 |\n"
                    "| --- | --- | --- | --- |\n"
                    "| 04h | 31.5℃ | 48% | 12.1 |\n"
                    "| 08h | 32.1℃ | 47% | 12.4 |\n"
                ),
            },
        ]

        res = match_book_to_template_pack(book, pages, tpl)
        assert res["coverage"]["verifiable_alert_count"] >= 3

        proc_out = next(s for s in res["sheets"] if s["sheet_id"] == "sheet_process")
        err_row_temp = next(
            r for r in proc_out["rows"] if r["batch_id"] == "B-ERR" and r["temp_c"] == 315.0
        )
        assert "越界异常: 温度=315" in err_row_temp["_meta"]["cell_alerts"]["temp_c"]
        assert "离群突变(>5倍)" in err_row_temp["_meta"]["cell_alerts"]["temp_c"]

        # target_metric has min_val/max_val = None, yet intra-batch >5x outlier catches 125.0 vs ~12.25
        err_row_metric = next(
            r for r in proc_out["rows"] if r["batch_id"] == "B-ERR" and r["target_metric"] == 125.0
        )
        assert "离群突变(>5倍)" in err_row_metric["_meta"]["cell_alerts"]["target_metric"]

        cb_rows = {r["batch_id"]: r for r in res["catboost_dataset"]["rows"]}
        assert cb_rows["B-ERR"]["review_required"] == 1
        assert cb_rows["B-OK"]["review_required"] == 0

        out_xlsx = tmp_path / "verifiable_out.xlsx"
        export_multisheet_pack_workbook(
            book=book,
            template=tpl,
            pages=pages,
            page_assignments={},
            out_path=out_xlsx,
        )

        wb = load_workbook(out_xlsx, data_only=True)
        try:
            ws_proc = wb["过程运行记录"]
            hdrs = [c.value for c in ws_proc[1]]
            temp_col = hdrs.index("温度") + 1
            batch_col = hdrs.index("批次号") + 1

            bad_cell = None
            good_cell = None
            for r_i in range(2, ws_proc.max_row + 1):
                bid = ws_proc.cell(row=r_i, column=batch_col).value
                tval = ws_proc.cell(row=r_i, column=temp_col).value
                cell = ws_proc.cell(row=r_i, column=temp_col)
                if bid == "B-ERR" and tval == 315:
                    bad_cell = cell
                elif bid == "B-OK" and tval == 31.5:
                    good_cell = cell

            assert bad_cell is not None
            assert bad_cell.fill.start_color.rgb.endswith("FEE2E2")
            assert bad_cell.font.color.rgb.endswith("991B1B")
            assert good_cell is not None
            assert not (good_cell.fill.fill_type == "solid" and good_cell.fill.start_color.rgb.endswith("FEE2E2"))

            ws_warn = wb["异常待复核"]
            warn_dump = "\n".join(str(c.value or "") for row in ws_warn.iter_rows() for c in row)
            assert "越界异常: 温度=315" in warn_dump
            assert "离群突变(>5倍)" in warn_dump

            ws_cb = wb["CatBoost_特征宽表"]
            cb_hdrs = [c.value for c in ws_cb[1]]
            rev_col = cb_hdrs.index("review_required") + 1
            bid_col = cb_hdrs.index("batch_id") + 1
            for r_i in range(2, ws_cb.max_row + 1):
                if ws_cb.cell(row=r_i, column=bid_col).value == "B-ERR":
                    rev_cell = ws_cb.cell(row=r_i, column=rev_col)
                    assert rev_cell.value == 1
                    assert rev_cell.fill.start_color.rgb.endswith("FEE2E2")
                elif ws_cb.cell(row=r_i, column=bid_col).value == "B-OK":
                    assert ws_cb.cell(row=r_i, column=rev_col).value == 0
        finally:
            wb.close()
    finally:
        db.close()


def test_api_field_bounds_persistence_and_match_preview_alerts(tmp_path):
    class _RangeStubOCR:
        def recognize(self, bgr, *, low_conf_threshold: float = 0.82) -> OCRResult:
            txt = "## 过程运行记录\n批次号: B-API\n\n| 记录时刻 | 温度 | 溶氧 |\n| --- | --- | --- |\n| 08h | 315℃ | 46% |"
            return OCRResult(
                blocks=[
                    OCRBlock(bbox=[[20, 20], [300, 20], [300, 50], [20, 50]], text="## 过程运行记录", confidence=0.98),
                ],
                raw_text=txt,
                formatted_text=txt,
                avg_confidence=0.97,
                min_confidence=0.96,
                flags=[],
                engine_used="rapidocr",
            )

    cfg = AppConfig(data_dir=tmp_path / "data", clean_workers=1, ocr_workers=1)
    app = create_app(cfg, start_workers=True, ocr_engine=_RangeStubOCR())

    with TestClient(app) as client:
        tpls = client.get("/api/excel-templates").json()["items"]
        pack = next(t for t in tpls if t.get("sheets"))

        # Update max_val on temp_c via PUT and verify persistence
        updated_sheets = []
        for s in pack["sheets"]:
            s_copy = dict(s)
            if s_copy["sheet_id"] == "sheet_process":
                s_copy["fields"] = [
                    {**f, "min_val": 10.0, "max_val": 80.0, "require_not_null": True}
                    if f["key"] == "temp_c"
                    else f
                    for f in s_copy["fields"]
                ]
            updated_sheets.append(s_copy)
        put_res = client.put(f"/api/excel-templates/{pack['id']}", json={"sheets": updated_sheets})
        assert put_res.status_code == 200
        saved_proc = next(s for s in put_res.json()["sheets"] if s["sheet_id"] == "sheet_process")
        saved_temp = next(f for f in saved_proc["fields"] if f["key"] == "temp_c")
        assert saved_temp["min_val"] == 10.0
        assert saved_temp["max_val"] == 80.0
        assert saved_temp["require_not_null"] is True

        book_id = client.post("/api/books", json={"title": "接口越界检验册"}).json()["id"]
        cap = client.post(
            f"/api/books/{book_id}/capture",
            files={"file": ("p1.jpg", _make_distinct_sharp_page(0), "image/jpeg")},
        )
        assert cap.status_code == 200
        page_id = cap.json()["pages"][0]["id"]

        deadline = time.time() + 10.0
        while time.time() < deadline:
            p = client.get(f"/api/pages/{page_id}").json()
            if p["status"] in ("READY", "NEEDS_REVIEW", "APPROVED"):
                break
            time.sleep(0.06)

        mp = client.get(f"/api/books/{book_id}/match-preview?template_id={pack['id']}").json()
        proc_mp = next(s for s in mp["sheets"] if s["sheet_id"] == "sheet_process")
        assert proc_mp["rows"][0]["_cell_alerts"]["temp_c"].startswith("越界异常: 温度=315")
        assert "10~80" in proc_mp["rows"][0]["_cell_alerts"]["temp_c"]
        assert mp["catboost_dataset"]["rows"][0]["review_required"] == 1


def test_smart_vlm_routing_triggers_and_olmocr_document_anchoring(tmp_path):
    # 1. Coordinate anchor formatting & ordering
    raw_blocks = [
        OCRBlock(bbox=[[220, 120], [310, 120], [310, 145], [220, 145]], text="315℃", confidence=0.72),
        OCRBlock(bbox=[[20, 120], [90, 120], [90, 145], [20, 145]], text="温度", confidence=0.95),
        OCRBlock(bbox=[[20, 40], [180, 40], [180, 70], [20, 70]], text="批号: B-01", confidence=0.97),
    ]
    anchor_str = build_coordinate_anchor_text(raw_blocks)
    lines = anchor_str.splitlines()
    assert lines[0] == "[20,40,180,70] 批号: B-01"
    assert lines[1] == "[20,120,90,145] 温度"
    assert lines[2] == "[220,120,310,145] 315℃ [low_conf]"

    # 2. Enhanced trigger detection
    cfg = OCRSettings(engine="hybrid_smart", hybrid_conf_threshold=0.85, hybrid_upgrade_tables=True)
    clean_res = OCRResult(
        blocks=[OCRBlock(bbox=[[20, 20], [400, 20], [400, 50], [20, 50]], text="标准说明正文内容", confidence=0.96)],
        raw_text="标准说明正文内容",
        formatted_text="标准说明正文内容",
        avg_confidence=0.96,
    )
    assert evaluate_hybrid_upgrade_triggers(clean_res, cfg) == []

    # High line fragmentation (10 tiny boxes across 2 rows)
    frag_blocks = []
    for row_y in (40, 90):
        for c in range(5):
            x0 = 20 + c * 90
            frag_blocks.append(OCRBlock(bbox=[[x0, row_y], [x0 + 30, row_y], [x0 + 30, row_y + 20], [x0, row_y + 20]], text=f"k{c}", confidence=0.94))
    frag_res = OCRResult(blocks=frag_blocks, raw_text="...", formatted_text="...", avg_confidence=0.94)
    frag_reasons = evaluate_hybrid_upgrade_triggers(frag_res, cfg)
    assert "high_line_fragmentation" in frag_reasons

    # Numeric anomaly (inline 315℃ temperature despite 0.95 overall conf)
    oob_res = OCRResult(
        blocks=[OCRBlock(bbox=[[20, 40], [220, 40], [220, 70], [20, 70]], text="温度: 315℃", confidence=0.95)],
        raw_text="温度: 315℃",
        formatted_text="温度: 315℃",
        avg_confidence=0.95,
    )
    assert "numeric_density_anomaly" in evaluate_hybrid_upgrade_triggers(oob_res, cfg)
    assert "out_of_range_alert" in evaluate_hybrid_upgrade_triggers(clean_res, cfg, extra_range_alerts=["越界异常"])

    # 3. Mocked SmartOCRRouter hybrid upgrade injects coordinate anchors into VLM prompt & records provenance
    db = Database(tmp_path / "router_anchor.db")
    try:
        captured_prompts: list[str] = []

        def vision_handler(req: httpx.Request) -> httpx.Response:
            payload = json.loads(req.content.decode("utf-8"))
            captured_prompts.append(payload["messages"][0]["content"][0]["text"])
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "| 参数 | 数值 |\n| --- | --- |\n| 温度 | 31.5℃ |"}}]},
            )

        stub_rapid = type(
            "_StubRapid",
            (),
            {
                "recognize": lambda self, bgr, *, low_conf_threshold=0.82: OCRResult(
                    blocks=frag_blocks,
                    raw_text="碎片手写表",
                    formatted_text="碎片手写表",
                    avg_confidence=0.93,
                    min_confidence=0.90,
                    flags=[],
                )
            },
        )()
        dummy_img = np.full((120, 200, 3), 255, dtype=np.uint8)

        with httpx.Client(transport=httpx.MockTransport(vision_handler)) as client:
            db.update_settings(
                {
                    "ocr": {
                        "engine": "hybrid_smart",
                        "strict_offline": False,
                        "api_base_url": "https://mock.vlm.local/v1",
                        "api_key": "sk-test",
                    }
                }
            )
            router = SmartOCRRouter(db=db, addon_manager=None, rapid_engine=stub_rapid, api_client=client)
            res = router.recognize(dummy_img)
            assert res.engine_used == "deepseek_ocr2_api"
            assert "high_line_fragmentation" in res.upgrade_reasons
            assert len(captured_prompts) == 1
            assert "Document Anchoring" in captured_prompts[0]
            assert "[20,40,50,60] k0" in captured_prompts[0]
            assert len(res.blocks) == len(frag_blocks)  # blocks preserved from rapid pass

        # Offline fallback with no external VLM stays 100% RapidOCR while keeping upgrade_reasons
        db.update_settings({"ocr": {"engine": "hybrid_smart", "strict_offline": True}})
        router_off = SmartOCRRouter(db=db, addon_manager=None, rapid_engine=stub_rapid)
        res_off = router_off.recognize(dummy_img)
        assert res_off.engine_used == "rapidocr"
        assert "high_line_fragmentation" in res_off.upgrade_reasons
    finally:
        db.close()

    # 4. extract_sheet_rows_with_llm receives coordinate_anchors when ocr_blocks provided
    llm_payloads: list[dict] = []

    def llm_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode("utf-8"))
        llm_payloads.append(json.loads(body["messages"][1]["content"]))
        return httpx.Response(
            200,
            json={"model": "mock-llm", "choices": [{"message": {"content": '{"rows": [{"batch_id": "B-01", "temp_c": 31.5}]}'}}]},
        )

    sheet_def = {
        "sheet_id": "sheet_process",
        "name": "过程运行记录",
        "mode": "table",
        "fields": [
            {"key": "batch_id", "name": "批次号", "role": "meta_id"},
            {"key": "temp_c", "name": "温度", "role": "num_feature", "min_val": 0.0, "max_val": 100.0},
        ],
    }
    llm_cfg = LLMConfig(mode="all", base_url="http://127.0.0.1:11434/v1", api_key="", model="local-qwen")
    with httpx.Client(transport=httpx.MockTransport(llm_handler)) as lclient:
        rows, _ = extract_sheet_rows_with_llm("批次 B-01\n温度 31.5", sheet_def, llm_cfg, client=lclient, ocr_blocks=raw_blocks)
    assert rows[0]["temp_c"] == 31.5
    assert "coordinate_anchors" in llm_payloads[0]
    assert "[220,120,310,145] 315℃ [low_conf]" in llm_payloads[0]["coordinate_anchors"]


def test_worker_engine_provenance_persisted_in_clean_meta(tmp_path):
    class _ProvenanceStub:
        def recognize(self, bgr, *, low_conf_threshold: float = 0.82) -> OCRResult:
            return OCRResult(
                blocks=[OCRBlock(bbox=[[10, 10], [200, 10], [200, 40], [10, 40]], text="本地视觉输出", confidence=0.93)],
                raw_text="本地视觉输出",
                formatted_text="本地视觉输出",
                avg_confidence=0.93,
                min_confidence=0.93,
                flags=[],
                engine_used="ollama_glm_ocr",
                upgrade_reasons=["complex_table"],
            )

    cfg = AppConfig(data_dir=tmp_path / "data", clean_workers=1, ocr_workers=1)
    app = create_app(cfg, start_workers=True, ocr_engine=_ProvenanceStub())
    with TestClient(app) as client:
        book_id = client.post("/api/books", json={"title": "引擎标记验证册"}).json()["id"]
        cap = client.post(
            f"/api/books/{book_id}/capture",
            files={"file": ("p.jpg", _make_distinct_sharp_page(1), "image/jpeg")},
        )
        page_id = cap.json()["pages"][0]["id"]
        deadline = time.time() + 10.0
        while time.time() < deadline:
            p = client.get(f"/api/pages/{page_id}").json()
            if p["status"] in ("READY", "NEEDS_REVIEW"):
                break
            time.sleep(0.06)
        page_out = client.get(f"/api/pages/{page_id}").json()
        assert page_out["clean_meta"]["ocr_engine_used"] == "ollama_glm_ocr"
        assert page_out["clean_meta"]["ocr_upgrade_reasons"] == ["complex_table"]


def test_consistent_pii_redaction_across_excel_markdown_and_jsonl(tmp_path):
    # 1. Direct unit test of ConsistentPIIRedactor stability & category mapping
    red = ConsistentPIIRedactor(enabled=True, custom_terms=["星辰工厂", "Project-Omega"])
    s1 = red.redact_text(
        "生产厂：星辰工厂；操作人：张三；复核人：李四；电话：13812345678；"
        "邮箱：zhangsan@example.com；身份证：110105199001011234；代号 Project-Omega。"
    )
    s2 = red.redact_text(
        "二车间（星辰工厂）由 操作人: 张三 交接，联系电话 +86 138-1234-5678。"
    )
    for raw_pii in ("张三", "李四", "13812345678", "138-1234-5678", "zhangsan@example.com", "110105199001011234", "星辰工厂", "Project-Omega"):
        assert raw_pii not in s1
        assert raw_pii not in s2

    # Same entity gets identical deterministic token across calls
    assert "TERM_01" in s1 and "TERM_01" in s2
    assert "PERSON_01" in s1 and "PERSON_01" in s2
    assert "PERSON_02" in s1
    assert "PHONE_01" in s1 and "PHONE_01" in s2
    assert "IDCARD_01" in s1
    assert "EMAIL_01" in s1

    stats_str = json.dumps(red.stats(), ensure_ascii=False)
    assert "张三" not in stats_str and "13812345678" not in stats_str and "星辰工厂" not in stats_str

    # 2. End-to-end API test: xlsx, markdown, jsonl all redact cleanly & keep CatBoost joinability
    class _PIIStubOCR:
        def __init__(self) -> None:
            self._texts = [
                (
                    "## 批记录台账\n"
                    "批次号: B-PII-1\n"
                    "设备编号: 星辰工厂-A线\n"
                    "操作人: 张三\n"
                    "联系: 13800138000 | qa@plant.local\n\n"
                    "| 时刻 | 温度 | 溶氧 |\n"
                    "| --- | --- | --- |\n"
                    "| 08h | 32.0℃ | 45% |"
                ),
                (
                    "## 批记录台账\n"
                    "批次号: B-PII-2\n"
                    "设备编号: 星辰工厂-A线\n"
                    "操作人: 张三\n"
                    "复核人: 李四\n"
                    "身份证: 31010119881212009X\n\n"
                    "| 时刻 | 温度 | 溶氧 |\n"
                    "| --- | --- | --- |\n"
                    "| 08h | 31.8℃ | 47% |"
                ),
            ]
            self._idx = 0

        def recognize(self, bgr, *, low_conf_threshold: float = 0.82) -> OCRResult:
            txt = self._texts[min(self._idx, len(self._texts) - 1)]
            self._idx += 1
            return OCRResult(
                blocks=[OCRBlock(bbox=[[20, 20], [400, 20], [400, 60], [20, 60]], text=txt.splitlines()[0], confidence=0.96)],
                raw_text=txt,
                formatted_text=txt,
                avg_confidence=0.96,
                min_confidence=0.96,
                flags=[],
                engine_used="rapidocr",
            )

    cfg = AppConfig(data_dir=tmp_path / "data", clean_workers=1, ocr_workers=1)
    app = create_app(cfg, start_workers=True, ocr_engine=_PIIStubOCR())

    with TestClient(app) as client:
        # Build custom template including 操作人 (signer cat_feature) + 过程运行记录
        tpl_res = client.post(
            "/api/excel-templates",
            json={
                "name": "脱敏批记录模板",
                "rules": {
                    "catboost_mode": True,
                    "redact_pii": True,
                    "custom_redact_terms": ["星辰工厂"],
                },
                "sheets": [
                    {
                        "sheet_id": "sheet_log",
                        "name": "批记录台账",
                        "mode": "record",
                        "match_keywords": ["批记录", "台账", "操作人"],
                        "fields": [
                            {"key": "batch_id", "name": "批次号", "role": "meta_id", "agg": "last", "aliases": ["批号"]},
                            {"key": "asset_id", "name": "设备编号", "role": "cat_feature", "agg": "last", "aliases": []},
                            {"key": "operator", "name": "操作人", "role": "cat_feature", "agg": "last", "aliases": ["操作员"]},
                            {"key": "temp_c", "name": "温度", "role": "num_feature", "agg": "last", "min_val": 0.0, "max_val": 100.0},
                        ],
                    }
                ],
            },
        )
        assert tpl_res.status_code == 201
        tpl_id = tpl_res.json()["id"]

        book_id = client.post("/api/books", json={"title": "星辰工厂保密批次册"}).json()["id"]
        for i in range(2):
            cap = client.post(
                f"/api/books/{book_id}/capture",
                files={"file": (f"p{i}.jpg", _make_distinct_sharp_page(i + 2), "image/jpeg")},
            )
            assert cap.status_code == 200

        deadline = time.time() + 10.0
        while time.time() < deadline:
            st = client.get(f"/api/stats?book_id={book_id}").json()["book"]
            if st["ready"] == 2:
                break
            time.sleep(0.06)
        assert client.get(f"/api/stats?book_id={book_id}").json()["book"]["ready"] == 2

        def _run_export(fmt: str) -> bytes:
            job = client.post(
                f"/api/books/{book_id}/export",
                json={"format": fmt, "template_id": tpl_id, "redact_pii": True, "custom_redact_terms": ["星辰工厂"]},
            ).json()["job"]
            end_t = time.time() + 10.0
            while time.time() < end_t:
                jd = client.get(f"/api/jobs/{job['id']}").json()
                if jd["status"] in ("DONE", "FAILED"):
                    assert jd["status"] == "DONE"
                    break
                time.sleep(0.05)
            dl = client.get(f"/api/jobs/{job['id']}/download")
            assert dl.status_code == 200
            return dl.content

        secrets = ("张三", "李四", "13800138000", "qa@plant.local", "31010119881212009X", "星辰工厂")

        # Excel check
        xlsx_bytes = _run_export("xlsx")
        wb = load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
        try:
            all_cells_text = "\n".join(
                str(c.value)
                for sname in wb.sheetnames
                for row in wb[sname].iter_rows()
                for c in row
                if c.value is not None
            )
            for s in secrets:
                assert s not in all_cells_text, f"Raw secret leaked in xlsx: {s}"

            ws_cb = wb["CatBoost_特征宽表"]
            cb_hdrs = [c.value for c in ws_cb[1]]
            r1 = dict(zip(cb_hdrs, [c.value for c in ws_cb[2]]))
            r2 = dict(zip(cb_hdrs, [c.value for c in ws_cb[3]]))
            # Both batches share identical PERSON_01 token for 张三 & TERM_01 for 星辰工厂!
            assert r1["操作人"] == r2["操作人"]
            assert str(r1["操作人"]).startswith("PERSON_")
            assert r1["设备编号"] == r2["设备编号"]
            assert "TERM_" in str(r1["设备编号"])
        finally:
            wb.close()

        # Markdown check
        md_text = _run_export("markdown").decode("utf-8")
        assert "pii_redacted: true" in md_text
        for s in secrets:
            assert s not in md_text, f"Raw secret leaked in md: {s}"
        assert "PERSON_" in md_text and "TERM_" in md_text

        # JSONL check
        jsonl_text = _run_export("jsonl").decode("utf-8")
        lines = [json.loads(ln) for ln in jsonl_text.splitlines() if ln.strip()]
        assert len(lines) == 2
        assert all(rec["pii_redacted"] is True for rec in lines)
        for s in secrets:
            assert s not in jsonl_text, f"Raw secret leaked in jsonl: {s}"
        assert lines[0]["text"].find("PERSON_01") >= 0
        assert lines[1]["text"].find("PERSON_01") >= 0
        assert lines[1]["text"].find("PERSON_02") >= 0
