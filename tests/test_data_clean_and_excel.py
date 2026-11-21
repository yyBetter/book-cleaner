"""Tests for Stage 2 mixed text/table data cleaning, custom Excel schemas, and multi-sheet .xlsx export."""

from __future__ import annotations

import io
import json
import time

import httpx
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from PIL import Image, ImageDraw

from book_cleaner.api import create_app
from book_cleaner.config import AppConfig
from book_cleaner.db import CATBOOST_BATCH_COLUMNS, CATBOOST_BATCH_RULES, DEFAULT_EXCEL_COLUMNS
from book_cleaner.demo_generator import generate_demo_pages
from book_cleaner.pipeline.data_clean import (
    CleaningRules,
    detect_running_headers,
    extract_book_structured_data,
    normalize_text,
    parse_numeric_value,
    sanitize_excel_text,
)
from book_cleaner.pipeline.excel_exporter import export_excel_workbook, read_headers_from_xlsx
from book_cleaner.pipeline.llm_clean import LLMConfig, extract_sheet_rows_with_llm
from book_cleaner.pipeline.ocr_engine import OCRBlock, OCRResult


def test_text_normalization_and_running_header_removal():
    rules = CleaningRules()
    raw = "２０２６年３月５日 编号ＡＢＣ－１２\u3000值：98.5％\ufffd\x01\x07\n\n\n2025/4/9"
    cleaned = normalize_text(raw, rules)
    assert "2026-03-05" in cleaned
    assert "ABC-12" in cleaned
    assert "98.5%" in cleaned
    assert "2025-04-09" in cleaned
    assert "\ufffd" not in cleaned
    assert sanitize_excel_text("\x00hello\x0bworld") == "helloworld"

    pages = [
        {"clean_text": "机密内部资料 严禁外传\n正文第一页\n第 1 页"},
        {"clean_text": "机密内部资料 严禁外传\n正文第二页\n第 2 页"},
        {"clean_text": "机密内部资料 严禁外传\n正文第三页\n第 3 页"},
        {"clean_text": "不同页眉\n正文第四页"},
    ]
    headers = detect_running_headers(pages, rules)
    assert "机密内部资料 严禁外传" in headers


def test_mixed_spatial_and_markdown_table_extraction_and_dedup():
    book = {"id": 1, "title": "工艺手册", "author": "Tester", "isbn": "978-0"}
    template = {
        "id": 1,
        "name": "测试模板",
        "entry_sheet_name": "正文结构化条目",
        "table_sheet_name": "提取表格明细",
        "rules": {
            "normalize_fullwidth": True,
            "normalize_dates": True,
            "strip_noise_symbols": True,
            "remove_running_headers": True,
            "deduplicate_entries": True,
            "min_confidence_warn": 0.85,
        },
        "columns": [
            *DEFAULT_EXCEL_COLUMNS,
            {"key": "batch_code", "header": "批次号", "source_type": "regex", "pattern": r"B-\d{4}", "required": True},
        ],
    }

    # Page 1: contains spatial OCR table + Markdown paragraph with chapter, date, KV, regex match
    pages = [
        {
            "id": 1,
            "seq_no": 1,
            "ocr_confidence": 0.94,
            "qa_flags": [],
            "source_ref": "p1.jpg",
            "clean_text": (
                "## 第一章 参数记录\n\n"
                "执行日期：2026年1月8日，批次：B-1024，温度：32.5℃。\n\n"
                "阶段  目标  备注\n"
                "灭菌  121℃  保持30分钟"
            ),
            "ocr_blocks": [
                {"bbox": [[50, 400], [180, 400], [180, 430], [50, 430]], "text": "阶段", "confidence": 0.95},
                {"bbox": [[350, 400], [480, 400], [480, 430], [350, 430]], "text": "目标", "confidence": 0.95},
                {"bbox": [[650, 400], [780, 400], [780, 430], [650, 430]], "text": "备注", "confidence": 0.95},
                {"bbox": [[50, 460], [180, 460], [180, 490], [50, 490]], "text": "灭菌", "confidence": 0.95},
                {"bbox": [[350, 460], [480, 460], [480, 490], [350, 490]], "text": "121℃", "confidence": 0.95},
                {"bbox": [[650, 460], [820, 460], [820, 490], [650, 490]], "text": "保持30分钟", "confidence": 0.95},
            ],
        },
        # Page 2: duplicate paragraph (should be deduplicated) + Markdown pipe table + low confidence warning + missing required regex
        {
            "id": 2,
            "seq_no": 2,
            "ocr_confidence": 0.71,
            "qa_flags": ["blurry"],
            "source_ref": "p2.jpg",
            "clean_text": (
                "执行日期：2026年1月8日，批次：B-1024，温度：32.5℃。\n\n"
                "本段缺少批次编码但有待核查参数：pH：6.8。\n\n"
                "| 项目 | 数值 |\n"
                "| --- | --- |\n"
                "| 溶氧 | 45% |"
            ),
            "ocr_blocks": [],
        },
    ]

    rows, summary = extract_book_structured_data(book, pages, template)
    entry_rows = [r for r in rows if r["row_kind"] == "ENTRY"]
    table_rows = [r for r in rows if r["row_kind"] == "TABLE"]

    assert summary["dedup_removed"] == 1
    assert summary["distinct_tables"] == 2
    assert len(entry_rows) == 2
    assert len(table_rows) == 4  # 2 headers + 2 data rows

    e1 = entry_rows[0]
    assert e1["data"]["chapter"] == "第一章 参数记录"
    assert e1["data"]["dates_found"] == "2026-01-08"
    assert e1["data"]["batch_code"] == "B-1024"
    assert "32.5℃" in e1["data"]["key_values"]
    assert e1["warnings"] == []

    e2 = entry_rows[1]
    assert e2["data"]["chapter"] == "第一章 参数记录"  # carried across pages
    assert any("OCR低置信度" in w for w in e2["warnings"])
    assert any("必填列缺失:批次号" in w for w in e2["warnings"])


def test_read_headers_from_xlsx_and_4sheet_excel_export_scale(tmp_path):
    # 1. Build a sample user .xlsx template and parse row-1 headers
    sample_wb = Workbook()
    sample_ws = sample_wb.active
    sample_ws.append(["书名", "页码", "所属章节", "清洗后正文", "批次编号"])
    buf = io.BytesIO()
    sample_wb.save(buf)

    parsed_cols = read_headers_from_xlsx(buf.getvalue())
    assert [c["header"] for c in parsed_cols] == ["书名", "页码", "所属章节", "清洗后正文", "批次编号"]
    assert parsed_cols[0]["source_type"] == "builtin" and parsed_cols[0]["pattern"] == "book_title"
    assert parsed_cols[3]["required"] is True
    assert parsed_cols[4]["source_type"] == "kv" and parsed_cols[4]["pattern"] == "批次编号"

    # 2. Scale & styling export test with 3,000 structured rows
    book = {"id": 9, "title": "三千条压测样书", "author": "QA"}
    tpl = {
        "id": 1,
        "name": "导出校验模板",
        "entry_sheet_name": "正文结构化条目",
        "table_sheet_name": "提取表格明细",
        "rules": {"normalize_fullwidth": True, "normalize_dates": True, "min_confidence_warn": 0.82},
        "columns": DEFAULT_EXCEL_COLUMNS,
    }
    rows = []
    for i in range(1, 3001):
        warns = ["OCR低置信度(71.0%)"] if i % 100 == 0 else []
        rows.append(
            {
                "row_kind": "ENTRY",
                "page_no": (i // 3) + 1,
                "table_index": 0,
                "row_index": i,
                "data": {
                    "book_title": "三千条压测样书",
                    "page_no": str((i // 3) + 1),
                    "chapter": "第一章",
                    "entry_title": f"条目{i}",
                    "clean_content": f"清洗后正文内容第{i}条\x00",
                    "dates_found": "2026-03-15",
                    "key_values": "压力: 0.2MPa",
                    "ocr_confidence": "92.0%",
                    "qa_status": "需复核" if warns else "通过",
                },
                "warnings": warns,
                "ocr_confidence": 0.71 if warns else 0.92,
            }
        )
    rows.append(
        {
            "row_kind": "TABLE",
            "page_no": 1,
            "table_index": 1,
            "row_index": 0,
            "data": {"is_header": True, "cells": ["阶段", "指标", "备注"]},
            "warnings": [],
            "ocr_confidence": 0.95,
        }
    )
    rows.append(
        {
            "row_kind": "TABLE",
            "page_no": 1,
            "table_index": 1,
            "row_index": 1,
            "data": {"is_header": False, "cells": ["L1清洗", "波动<5", "正常"]},
            "warnings": [],
            "ocr_confidence": 0.95,
        }
    )

    out_xlsx = tmp_path / "scale_3k.xlsx"
    summary = {
        "pages_processed": 1000,
        "entries": 3000,
        "distinct_tables": 1,
        "table_rows": 2,
        "dedup_removed": 5,
        "warning_rows": 30,
        "pass_rate_pct": 99.0,
        "running_headers_stripped": [],
    }
    export_excel_workbook(book, tpl, rows, summary, out_xlsx)

    wb_check = load_workbook(out_xlsx, read_only=False)
    try:
        assert wb_check.sheetnames == ["正文结构化条目", "提取表格明细", "异常待复核", "清洗汇总报告"]
        ws_e = wb_check["正文结构化条目"]
        assert ws_e.max_row == 3001  # header + 3000 rows
        # Row 101 corresponds to i=100 (warning row -> amber fill FFF3CD)
        assert ws_e.cell(row=101, column=1).fill.start_color.rgb.endswith("FFF3CD")
        assert ws_e.cell(row=101, column=5).value == "清洗后正文内容第100条"

        ws_t = wb_check["提取表格明细"]
        assert ws_t.max_row == 3
        assert ws_t.cell(row=2, column=4).value == "阶段"

        ws_w = wb_check["异常待复核"]
        assert ws_w.max_row == 31  # header + 30 warning rows
    finally:
        wb_check.close()


class _TableStubOCR:
    def recognize(self, bgr, *, low_conf_threshold: float = 0.82) -> OCRResult:
        return OCRResult(
            blocks=[
                OCRBlock(bbox=[[40, 40], [500, 40], [500, 90], [40, 90]], text="第五章 质量门禁与断点续传", confidence=0.96),
                OCRBlock(bbox=[[40, 130], [700, 130], [700, 175], [40, 175]], text="制定日期：２０２６年３月１５日，清晰度下限：80.0。", confidence=0.95),
            ],
            raw_text="第五章 质量门禁与断点续传\n制定日期：２０２６年３月１５日，清晰度下限：80.0。\n\n工序阶段  目标指标  备注说明\nL0采集质检  清晰度>=80  模糊即时蜂鸣",
            formatted_text="## 第五章 质量门禁与断点续传\n\n制定日期：２０２６年３月１５日，清晰度下限：80.0。\n\n工序阶段  目标指标  备注说明\nL0采集质检  清晰度>=80  模糊即时蜂鸣",
            avg_confidence=0.95,
            min_confidence=0.95,
            flags=[],
        )


def test_api_stage2_dataclean_and_xlsx_download(tmp_path):
    cfg = AppConfig(data_dir=tmp_path / "data", clean_workers=1, ocr_workers=1)
    app = create_app(cfg, start_workers=True, ocr_engine=_TableStubOCR())
    samples = dict(generate_demo_pages())

    with TestClient(app) as client:
        # 1. Verify templates CRUD & .xlsx upload
        tpls = client.get("/api/excel-templates").json()["items"]
        assert len(tpls) >= 1

        wb_up = Workbook()
        wb_up.active.append(["书名", "页码", "清洗后正文", "清晰度下限"])
        up_buf = io.BytesIO()
        wb_up.save(up_buf)
        from_xlsx = client.post(
            "/api/excel-templates/from-xlsx",
            files={"file": ("my_schema.xlsx", up_buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"name": "上传表头测试模板"},
        )
        assert from_xlsx.status_code == 201
        tpl_id = from_xlsx.json()["id"]

        # 2. Ingest page & wait for OCR
        book = client.post("/api/books", json={"title": "阶段二出表验证册"}).json()
        book_id = book["id"]
        cap = client.post(
            f"/api/books/{book_id}/capture",
            files={"file": ("p4.jpg", samples["04_tilted_clean_page.jpg"], "image/jpeg")},
        )
        assert cap.status_code == 200
        deadline = time.time() + 10.0
        while time.time() < deadline:
            st = client.get(f"/api/stats?book_id={book_id}").json()["book"]
            if st["ready"] == 1:
                break
            time.sleep(0.08)
        assert client.get(f"/api/stats?book_id={book_id}").json()["book"]["ready"] == 1

        # 3. Run Stage 2 cleaning & check structured-rows API
        run_res = client.post(
            f"/api/books/{book_id}/data-clean/run",
            json={"template_id": tpl_id},
        )
        assert run_res.status_code == 200
        summary = run_res.json()["summary"]
        assert summary["entries"] >= 1
        assert summary["distinct_tables"] == 1

        entries = client.get(f"/api/books/{book_id}/structured-rows?kind=ENTRY").json()
        assert entries["total"] >= 1
        assert entries["items"][0]["data"]["清晰度下限"].startswith("80.0")

        # 4. Export multi-sheet .xlsx and verify workbook contents
        exp_job = client.post(
            f"/api/books/{book_id}/export",
            json={"format": "xlsx", "template_id": tpl_id},
        ).json()["job"]
        deadline = time.time() + 10.0
        while time.time() < deadline:
            jd = client.get(f"/api/jobs/{exp_job['id']}").json()
            if jd["status"] in ("DONE", "FAILED"):
                break
            time.sleep(0.05)
        assert client.get(f"/api/jobs/{exp_job['id']}").json()["status"] == "DONE"

        dl = client.get(f"/api/jobs/{exp_job['id']}/download")
        assert dl.status_code == 200
        wb_out = load_workbook(io.BytesIO(dl.content))
        try:
            assert wb_out.sheetnames == ["正文结构化条目", "提取表格明细", "异常待复核", "清洗汇总报告"]
            ws_t = wb_out["提取表格明细"]
            assert ws_t.cell(row=2, column=4).value == "工序阶段"
            assert ws_t.cell(row=3, column=5).value == "清晰度>=80"
        finally:
            wb_out.close()


def test_catboost_batch_sop_and_table_excel_contract(tmp_path):
    assert parse_numeric_value("32.5℃") == 32.5
    assert parse_numeric_value("98.4%") == 98.4
    assert parse_numeric_value("0.18 MPa") == 0.18
    assert parse_numeric_value("2026-03-15") is None
    assert parse_numeric_value("-") is None

    book = {"id": 9, "title": "生产批记录汇编", "isbn": "LINE-A-01"}
    tpl = {
        "id": 2,
        "name": "批次记录与 CatBoost 建模标准模板",
        "entry_sheet_name": "正文结构化条目",
        "table_sheet_name": "提取表格明细",
        "rules": dict(CATBOOST_BATCH_RULES),
        "columns": list(CATBOOST_BATCH_COLUMNS),
    }
    pages = [
        {
            "id": 101,
            "seq_no": 1,
            "ocr_confidence": 0.96,
            "qa_flags": [],
            "clean_text": (
                "## SOP操作规范\n"
                "批次号: B20261101\n"
                "日期: 2026年11月1日\n"
                "标准操作步骤：严格控制罐温 31.0℃~32.5℃。\n\n"
                "| 时刻 | 温度 | 纯度 | 压力 |\n"
                "| --- | --- | --- | --- |\n"
                "| 08h | 31.5℃ | 83.2% | 0.16 MPa |\n"
                "| 12h | 32.5℃ | 85.4% | 0.18 MPa |\n"
            ),
        },
        {
            "id": 102,
            "seq_no": 2,
            "ocr_confidence": 0.92,
            "qa_flags": [],
            "clean_text": (
                "## 异常交接记录\n"
                "批次号: B20261102\n"
                "2026-11-02 夜班微调补糖流速，过程指标稳态。\n\n"
                "| 时刻 | 温度 | 纯度 |\n"
                "| --- | --- | --- |\n"
                "| 04h | 30.8℃ | 81.6% |\n"
            ),
        },
    ]

    rows, summary = extract_book_structured_data(book, pages, tpl)
    assert summary["entries"] >= 2
    assert summary["distinct_tables"] == 2

    out_xlsx = tmp_path / "catboost_ready.xlsx"
    export_excel_workbook(book, tpl, rows, summary, out_xlsx)

    wb = load_workbook(out_xlsx, data_only=True)
    try:
        assert wb.sheetnames == [
            "正文结构化条目",
            "提取表格明细",
            "异常待复核",
            "清洗汇总报告",
            "CatBoost_特征宽表",
            "CatBoost_字段契约",
        ]
        ws_cb = wb["CatBoost_特征宽表"]
        headers = [c.value for c in ws_cb[1]]
        assert "batch_id" in headers
        assert "温度_mean" in headers
        assert "纯度_max" in headers
        assert "压力_last" in headers
        assert "sop_notes_text" in headers
        assert "review_required" in headers
        assert ws_cb.max_row == 3  # 2 batches + header

        row1 = dict(zip(headers, [c.value for c in ws_cb[2]]))
        row2 = dict(zip(headers, [c.value for c in ws_cb[3]]))
        assert row1["batch_id"] == "B20261101"
        assert row1["record_date"] == "2026-11-01"
        assert abs(float(row1["温度_mean"]) - 32.0) < 1e-6
        assert abs(float(row1["纯度_max"]) - 85.4) < 1e-6
        assert abs(float(row1["压力_last"]) - 0.18) < 1e-6
        assert row1["sop_entry_count"] >= 1
        assert row1["review_required"] == 0

        assert row2["batch_id"] == "B20261102"
        assert row2["压力_last"] is None  # missing stays native blank/NaN for CatBoost
        assert row2["anomaly_note_count"] >= 1

        ws_schema = wb["CatBoost_字段契约"]
        schema_cols = [ws_schema.cell(row=r, column=1).value for r in range(2, ws_schema.max_row + 1)]
        assert schema_cols == headers
    finally:
        wb.close()


class _MultiSheetRouterStubOCR:
    def __init__(self) -> None:
        self._texts = [
            "## 批次基础档案\n批次号: B20261101\n日期: 2026-11-01\n设备编号: LINE-01\n投料量: 1200 kg",
            "## 过程运行记录\n批次号: B20261101\n\n| 记录时刻 | 奇葩温度列 | 溶氧 | 备注 |\n| --- | --- | --- | --- |\n| 08h | 31.5℃ | 46% | 正常 |\n| 12h | 32.5℃ | 44% | 平稳 |",
            "## 化验检测记录\n批次号: B20261101\n\n| 取样时刻 | 化验浓度 | 镜检结果 |\n| --- | --- | --- |\n| 12h | 18.4 g/L | 正常 |",
            "## SOP与异常交接\n批次号: B20261101\n日期: 2026-11-01\n标准操作步骤：前10h缓慢升温并观察溶氧曲线。",
            "## 批次结果_Y\n批次号: B20261101\n完成日期: 2026-11-03\ny_效价: 4820 U/mL\ny_纯度: 85.2%\ny_收率: 91.5%",
            "## 批次结果_Y\n批次号: B20261102\n完成日期: 2026-11-05\ny_效价: 4510 U/mL\ny_纯度: 83.1%",
            "补充手写便签：备品备件领用记录待归类",
        ]
        self._idx = 0

    def recognize(self, bgr, *, low_conf_threshold: float = 0.82) -> OCRResult:
        txt = self._texts[min(self._idx, len(self._texts) - 1)]
        self._idx += 1
        return OCRResult(
            blocks=[OCRBlock(bbox=[[20, 20], [500, 20], [500, 70], [20, 70]], text=txt.splitlines()[0], confidence=0.96)],
            raw_text=txt,
            formatted_text=txt,
            avg_confidence=0.95,
            min_confidence=0.94,
            flags=[],
        )


def test_multisheet_xlsx_import_routing_alias_learning_and_multi_y_catboost(tmp_path):
    # 1. Build a 5-sheet blank Excel template pack with 3 target_y columns
    wb_tpl = Workbook()
    ws1 = wb_tpl.active
    ws1.title = "批次基础档案"
    ws1.append(["批次号", "日期", "设备编号", "投料量"])
    ws2 = wb_tpl.create_sheet("过程运行记录")
    ws2.append(["批次号", "记录时刻", "温度", "溶氧", "备注"])
    ws3 = wb_tpl.create_sheet("化验检测记录")
    ws3.append(["批次号", "取样时刻", "化验浓度", "镜检结果"])
    ws4 = wb_tpl.create_sheet("SOP与异常交接")
    ws4.append(["批次号", "日期", "记录小节", "SOP与异常正文"])
    ws5 = wb_tpl.create_sheet("批次结果_Y")
    ws5.append(["批次号", "完成日期", "y_效价", "y_纯度", "y_收率"])
    buf = io.BytesIO()
    wb_tpl.save(buf)

    cfg = AppConfig(data_dir=tmp_path / "data", clean_workers=1, ocr_workers=1)
    app = create_app(cfg, start_workers=True, ocr_engine=_MultiSheetRouterStubOCR())

    def _make_distinct_sharp_page(idx: int) -> bytes:
        im = Image.new("RGB", (800, 1100), (248, 248, 246))
        draw = ImageDraw.Draw(im)
        # Distinct high-contrast geometric pattern per page so blur_score >> 80 and dHash is unique
        for row in range(12):
            if (row + idx) % 2 == 0 or (idx & (1 << (row % 4))):
                y0 = 80 + row * 78 + (idx * 7) % 19
                x0 = 90 + ((idx * 53 + row * 31) % 240)
                w = 220 + ((idx + row) % 3) * 110
                draw.rectangle([x0, y0, min(720, x0 + w), y0 + 26], fill=(24, 24, 27))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=92)
        return out.getvalue()

    with TestClient(app) as client:
        up = client.post(
            "/api/excel-templates/from-xlsx",
            files={
                "file": (
                    "multi_batch_pack.xlsx",
                    buf.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            data={"name": "五表多目标模板包"},
        )
        assert up.status_code == 201
        tpl = up.json()
        tpl_id = tpl["id"]
        assert len(tpl["sheets"]) == 5
        res_sheet = next(s for s in tpl["sheets"] if s["name"] == "批次结果_Y")
        assert [f["name"] for f in res_sheet["fields"] if f["role"] == "target_y"] == ["y_效价", "y_纯度", "y_收率"]

        # 2. Ingest 7 distinct pages (5 types for Batch 1 + 1 partial results for Batch 2 + 1 unassigned ambiguous note)
        book = client.post("/api/books", json={"title": "多表分拣验证批次册"}).json()
        book_id = book["id"]
        page_ids = []
        for idx in range(7):
            cap = client.post(
                f"/api/books/{book_id}/capture",
                files={"file": (f"p_{idx + 1}.jpg", _make_distinct_sharp_page(idx), "image/jpeg")},
            )
            assert cap.status_code == 200
            page_ids.append(cap.json()["pages"][0]["id"])

        deadline = time.time() + 12.0
        while time.time() < deadline:
            st = client.get(f"/api/stats?book_id={book_id}").json()["book"]
            if st["ready"] == 7:
                break
            time.sleep(0.08)
        assert client.get(f"/api/stats?book_id={book_id}").json()["book"]["ready"] == 7

        # 3. Inspect match-preview before alias learning
        mp1 = client.get(f"/api/books/{book_id}/match-preview?template_id={tpl_id}").json()
        assert mp1["coverage"]["matched_pages"] >= 6
        proc_sheet = next(s for s in mp1["sheets"] if s["name"] == "过程运行记录")
        temp_field = next(f for f in proc_sheet["fields"] if f["name"] == "温度")
        quirk_map = next((m for m in proc_sheet["column_mappings"] if m["source_col"] == "奇葩温度列"), None)
        assert quirk_map is not None
        assert quirk_map["status"] == "unmatched"

        # 4. Learn alias ("奇葩温度列" -> 温度) and verify immediate re-alignment & numeric extraction
        learned = client.post(
            f"/api/excel-templates/{tpl_id}/learn-alias",
            json={
                "sheet_id": proc_sheet["sheet_id"],
                "field_key": temp_field["key"],
                "source_alias": "奇葩温度列",
            },
        )
        assert learned.status_code == 200
        mp2 = client.get(f"/api/books/{book_id}/match-preview?template_id={tpl_id}").json()
        proc2 = next(s for s in mp2["sheets"] if s["name"] == "过程运行记录")
        quirk_map2 = next(m for m in proc2["column_mappings"] if m["source_col"] == "奇葩温度列")
        assert quirk_map2["status"] == "matched_alias"
        assert any(r.get(temp_field["key"]) == 31.5 for r in proc2["rows"])

        # 5. Manually override page #7 into SOP sheet via page-assignments endpoint
        sop_sheet = next(s for s in mp2["sheets"] if s["name"] == "SOP与异常交接")
        assign_res = client.put(
            f"/api/books/{book_id}/page-assignments",
            json={"template_id": tpl_id, "page_id": page_ids[6], "sheet_id": sop_sheet["sheet_id"]},
        )
        assert assign_res.status_code == 200
        mp3 = assign_res.json()
        sop3 = next(s for s in mp3["sheets"] if s["name"] == "SOP与异常交接")
        assert any(p["page_id"] == page_ids[6] and p["match_source"] == "manual" for p in sop3["assigned_pages"])

        # 6. Export full workbook and inspect all sheets + CatBoost wide table & multi-y schema contract
        exp = client.post(
            f"/api/books/{book_id}/export",
            json={"format": "xlsx", "template_id": tpl_id},
        ).json()["job"]
        deadline = time.time() + 12.0
        while time.time() < deadline:
            jd = client.get(f"/api/jobs/{exp['id']}").json()
            if jd["status"] in ("DONE", "FAILED"):
                break
            time.sleep(0.05)
        assert client.get(f"/api/jobs/{exp['id']}").json()["status"] == "DONE"

        dl = client.get(f"/api/jobs/{exp['id']}/download")
        assert dl.status_code == 200
        wb_out = load_workbook(io.BytesIO(dl.content), data_only=True)
        try:
            assert wb_out.sheetnames == [
                "批次基础档案",
                "过程运行记录",
                "化验检测记录",
                "SOP与异常交接",
                "批次结果_Y",
                "异常待复核",
                "清洗汇总报告",
                "CatBoost_特征宽表",
                "CatBoost_字段契约",
            ]
            for sname in ("批次基础档案", "过程运行记录", "批次结果_Y", "CatBoost_特征宽表"):
                assert len(wb_out[sname].merged_cells.ranges) == 0

            ws_cb = wb_out["CatBoost_特征宽表"]
            headers = [c.value for c in ws_cb[1]]
            assert headers[-3:] == ["y_效价", "y_纯度", "y_收率"]
            assert ws_cb.max_row == 3  # header + 2 batches
            b1 = dict(zip(headers, [c.value for c in ws_cb[2]]))
            b2 = dict(zip(headers, [c.value for c in ws_cb[3]]))
            assert b1["batch_id"] == "B20261101"
            assert abs(float(b1["温度_mean"]) - 32.0) < 1e-6
            assert abs(float(b1["投料量"]) - 1200.0) < 1e-6
            assert abs(float(b1["y_效价"]) - 4820.0) < 1e-6
            assert abs(float(b1["y_纯度"]) - 85.2) < 1e-6
            assert abs(float(b1["y_收率"]) - 91.5) < 1e-6

            assert b2["batch_id"] == "B20261102"
            assert b2["温度_mean"] is None  # missing process stays true blank / NaN
            assert b2["y_收率"] is None

            ws_schema = wb_out["CatBoost_字段契约"]
            role_by_col = {
                ws_schema.cell(row=r, column=1).value: ws_schema.cell(row=r, column=2).value
                for r in range(2, ws_schema.max_row + 1)
            }
            assert role_by_col["y_效价"] == "target_y"
            assert role_by_col["y_纯度"] == "target_y"
            assert role_by_col["y_收率"] == "target_y"
            assert role_by_col["温度_mean"] == "num_feature"
            assert role_by_col["review_required"] == "qa_gate"
        finally:
            wb_out.close()


def test_llm_sheet_extract_mocked():
    sheet_def = {
        "sheet_id": "sheet_proc",
        "name": "过程运行记录",
        "mode": "table",
        "fields": [
            {"key": "batch_id", "name": "批次号", "role": "meta_id"},
            {"key": "temp_c", "name": "温度", "role": "num_feature"},
            {"key": "do_pct", "name": "溶氧", "role": "num_feature"},
        ],
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "mock-gpt",
                "choices": [
                    {
                        "message": {
                            "content": '```json\n{"rows": [{"batch_id": "B-99", "温度": 32.1, "do_pct": null}]}\n```'
                        }
                    }
                ],
            },
        )

    cfg = LLMConfig(mode="all", base_url="https://api.example.com/v1", api_key="sk-test", model="mock-gpt")
    with httpx.Client(transport=httpx.MockTransport(_handler)) as client:
        rows, meta = extract_sheet_rows_with_llm("批次 B-99，温度 32.1℃", sheet_def, cfg, client=client)

    assert meta["model"] == "mock-gpt"
    assert len(rows) == 1
    assert rows[0]["batch_id"] == "B-99"
    assert rows[0]["temp_c"] == 32.1
    assert rows[0]["do_pct"] is None


