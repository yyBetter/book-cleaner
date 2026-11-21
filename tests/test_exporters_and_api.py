"""End-to-end tests for Searchable PDF/MD/EPUB/JSONL exporters and FastAPI Workbench API."""

from __future__ import annotations

import json
import time
import zipfile

import pymupdf
from fastapi.testclient import TestClient

from book_cleaner.api import _score_lan_candidate, create_app
from book_cleaner.config import AppConfig
from book_cleaner.demo_generator import generate_demo_pages, write_demo_book_to_dir
from book_cleaner.pipeline.ocr_engine import OCRBlock, OCRResult


class _FastStubOCREngine:
    """Deterministic fast OCR stub for API integration & exporter tests."""

    def recognize(self, bgr, *, low_conf_threshold: float = 0.82) -> OCRResult:
        blocks = [
            OCRBlock(bbox=[[40, 40], [500, 40], [500, 90], [40, 90]], text="第一章 纸质书籍数字化", confidence=0.96),
            OCRBlock(bbox=[[40, 120], [540, 120], [540, 165], [40, 165]], text="本页验证导出检索与排版。", confidence=0.94),
        ]
        return OCRResult(
            blocks=blocks,
            raw_text="第一章 纸质书籍数字化\n本页验证导出检索与排版。",
            formatted_text="## 第一章 纸质书籍数字化\n\n本页验证导出检索与排版。",
            avg_confidence=0.95,
            min_confidence=0.94,
            flags=[],
        )


def _wait_until_settled(client: TestClient, book_id: int, expected_min: int, timeout_s: float = 15.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        stats = client.get(f"/api/stats?book_id={book_id}").json()["book"]
        done = stats["completed"] + stats["needs_review"] + stats["failed"]
        if stats["total"] >= expected_min and done == stats["total"]:
            return stats
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for background worker pipeline to settle")


def test_full_api_workflow_and_all_exporters(tmp_path):
    cfg = AppConfig(data_dir=tmp_path / "data", clean_workers=2, ocr_workers=1)
    app = create_app(cfg, start_workers=True, ocr_engine=_FastStubOCREngine())

    samples = dict(generate_demo_pages())
    import_dir = tmp_path / "bulk_scans"
    write_demo_book_to_dir(import_dir)

    with TestClient(app) as client:
        # 1. Health, UI routes & Mobile QR
        assert client.get("/api/health").status_code == 200
        assert client.get("/").status_code == 200
        assert client.get("/mobile").status_code == 200

        m_info = client.get("/api/mobile/info?book_id=1")
        assert m_info.status_code == 200
        assert "/mobile?book_id=1" in m_info.json()["url"]

        qr_resp = client.get("/api/mobile/qr?book_id=1")
        assert qr_resp.status_code == 200
        assert "svg" in qr_resp.headers["content-type"]
        assert "<svg" in qr_resp.text

        # 2. Create book & capture spread + single page
        book = client.post("/api/books", json={"title": "端到端集成测试册", "author": "Test"}).json()
        book_id = book["id"]

        cap1 = client.post(
            f"/api/books/{book_id}/capture",
            files={"file": ("spread.jpg", samples["03_double_spread.jpg"], "image/jpeg")},
            data={"split_spread": "true", "enhance_mode": "clean_color"},
        )
        assert cap1.status_code == 200
        assert cap1.json()["split_count"] == 2

        cap2 = client.post(
            f"/api/books/{book_id}/capture",
            files={"file": ("blur.jpg", samples["05_blurry_test_shot.jpg"], "image/jpeg")},
            data={"split_spread": "false"},
        )
        assert cap2.status_code == 200
        assert "blurry" in cap2.json()["qa"]["flags"]

        stats = _wait_until_settled(client, book_id, expected_min=3)
        assert stats["total"] == 3
        assert stats["needs_review"] == 1  # blurry page routed to NEEDS_REVIEW
        assert stats["ready"] == 2

        # 3. Inspect, reclean & approve page
        pages = client.get(f"/api/books/{book_id}/pages?status=ALL").json()["items"]
        first_id = pages[0]["id"]
        assert client.get(f"/api/pages/{first_id}/image/raw").status_code == 200
        assert client.get(f"/api/pages/{first_id}/image/clean").status_code == 200
        assert client.get(f"/api/pages/{first_id}/image/thumb").status_code == 200

        reclean = client.post(
            f"/api/pages/{first_id}/reclean",
            json={"profile_patch": {"enhance_mode": "bw_sauvola"}, "rerun_ocr": False},
        )
        assert reclean.status_code == 200

        approved = client.patch(
            f"/api/pages/{first_id}",
            json={"clean_text": "## 第一章 人工复核终稿\n\n验证导出检索层。", "status": "APPROVED"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "APPROVED"

        # 4. Export all 4 formats & validate file contents
        exported_files = {}
        for fmt in ("pdf", "markdown", "epub", "jsonl"):
            job_id = client.post(f"/api/books/{book_id}/export", json={"format": fmt}).json()["job"]["id"]
            deadline = time.time() + 10.0
            job_data = None
            while time.time() < deadline:
                job_data = client.get(f"/api/jobs/{job_id}").json()
                if job_data["status"] in ("DONE", "FAILED"):
                    break
                time.sleep(0.05)
            assert job_data is not None and job_data["status"] == "DONE"
            dl = client.get(f"/api/jobs/{job_id}/download")
            assert dl.status_code == 200
            exported_files[fmt] = dl.content

        # Verify Searchable PDF embeds readable CJK text
        pdf_doc = pymupdf.open(stream=exported_files["pdf"], filetype="pdf")
        assert len(pdf_doc) == 3
        extracted = "".join(pdf_doc[i].get_text() for i in range(len(pdf_doc)))
        pdf_doc.close()
        assert "人工复核终稿" in extracted
        assert "纸质书籍数字化" in extracted

        # Verify Markdown
        md_text = exported_files["markdown"].decode("utf-8")
        assert 'title: "端到端集成测试册"' in md_text
        assert "<!-- page: 1" in md_text
        assert "人工复核终稿" in md_text

        # Verify EPUB 3 archive structure
        epub_path = tmp_path / "out.epub"
        epub_path.write_bytes(exported_files["epub"])
        with zipfile.ZipFile(epub_path, "r") as zf:
            names = zf.namelist()
            assert names[0] == "mimetype"
            assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
            assert zf.read("mimetype") == b"application/epub+zip"
            assert "META-INF/container.xml" in names
            assert "OEBPS/content.opf" in names
            assert "OEBPS/nav.xhtml" in names
            assert "OEBPS/toc.ncx" in names
            assert "OEBPS/chap1.xhtml" in names

        # Verify JSONL
        jsonl_lines = [
            json.loads(ln) for ln in exported_files["jsonl"].decode("utf-8").splitlines() if ln.strip()
        ]
        assert len(jsonl_lines) == 3
        assert jsonl_lines[0]["book_id"] == book_id
        assert "人工复核终稿" in jsonl_lines[0]["text"]

        # 5. Bulk Folder Import endpoint
        imp_book = client.post("/api/books", json={"title": "批量目录测试"}).json()
        imp_job = client.post(
            f"/api/books/{imp_book['id']}/import-folder",
            json={"folder_path": str(import_dir), "recursive": True},
        ).json()["job"]
        deadline = time.time() + 15.0
        while time.time() < deadline:
            jd = client.get(f"/api/jobs/{imp_job['id']}").json()
            if jd["status"] == "DONE":
                break
            time.sleep(0.1)
        assert client.get(f"/api/jobs/{imp_job['id']}").json()["done_items"] == 6
        _wait_until_settled(client, imp_book["id"], expected_min=6)


def test_lan_ip_scoring_skips_vpn_tun():
    assert _score_lan_candidate("utun4", "198.18.0.1") is None
    assert _score_lan_candidate("lo0", "127.0.0.1") is None
    assert _score_lan_candidate("en0", "192.168.1.4") > _score_lan_candidate("en5", "10.0.0.5")


def test_waste_page_removal_batch_flagged_clear_and_book_fallback(tmp_path):
    cfg = AppConfig(data_dir=tmp_path / "data", clean_workers=1, ocr_workers=1)
    app = create_app(cfg, start_workers=True, ocr_engine=_FastStubOCREngine())
    samples = dict(generate_demo_pages())

    with TestClient(app) as client:
        book = client.post("/api/books", json={"title": "删废测试册"}).json()
        bid = book["id"]
        for fname in ("03_double_spread.jpg", "05_blurry_test_shot.jpg", "04_tilted_clean_page.jpg"):
            res = client.post(
                f"/api/books/{bid}/capture",
                files={"file": (fname, samples[fname], "image/jpeg")},
            )
            assert res.status_code == 200
        _wait_until_settled(client, bid, expected_min=3)

        # Verify desc order returns newest first
        desc_res = client.get(f"/api/books/{bid}/pages?order=desc&limit=10").json()
        seqs = [p["seq_no"] for p in desc_res["items"]]
        assert seqs == sorted(seqs, reverse=True)

        # Flagged cleanup removes blurry page (#2)
        del_flag = client.post(f"/api/books/{bid}/pages/delete-flagged").json()
        assert del_flag["deleted_count"] >= 1

        # Batch delete remaining first page
        left_pages = client.get(f"/api/books/{bid}/pages").json()["items"]
        assert len(left_pages) >= 1
        del_batch = client.post(
            f"/api/books/{bid}/pages/batch-delete",
            json={"page_ids": [left_pages[0]["id"]]},
        ).json()
        assert del_batch["deleted_count"] == 1

        # Clear remaining pages in book
        clr = client.delete(f"/api/books/{bid}/pages").json()
        assert clr["deleted_count"] == len(left_pages) - 1
        assert client.get(f"/api/books/{bid}/pages").json()["total"] == 0

        # Deleting a book returns fallback_book_id so UI never loses context
        del_book = client.delete(f"/api/books/{bid}").json()
        assert del_book["deleted"] is True
        assert del_book["fallback_book_id"] >= 1


