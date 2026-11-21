"""Tests for Stage 2 mixed text/table data cleaning, custom Excel schemas, and multi-sheet .xlsx export."""

from __future__ import annotations

import io
import time

from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from book_cleaner.api import create_app
from book_cleaner.config import AppConfig
from book_cleaner.db import DEFAULT_EXCEL_COLUMNS
from book_cleaner.demo_generator import generate_demo_pages
from book_cleaner.pipeline.data_clean import (
    CleaningRules,
    detect_running_headers,
    extract_book_structured_data,
    normalize_text,
    sanitize_excel_text,
)
from book_cleaner.pipeline.excel_exporter import export_excel_workbook, read_headers_from_xlsx
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
