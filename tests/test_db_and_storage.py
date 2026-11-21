"""Tests for sharded storage, EXIF normalization, WAL crash recovery, and 10k-row SQLite speed."""

from __future__ import annotations

import io
import sqlite3
import time

from PIL import Image

from book_cleaner.db import Database, utc_now_iso
from book_cleaner.storage import ShardedStorage, compute_sha256, decode_image_with_exif


def test_sharded_storage_and_exif_transpose(tmp_path):
    storage = ShardedStorage(tmp_path)
    raw_bytes = b"sample-page-bytes"
    sha = compute_sha256(raw_bytes)
    paths = storage.page_paths(book_id=3, page_id=42, sha256=sha)

    assert paths["raw"].parent.name == sha[:2]
    assert paths["raw"].parent.parent.name == "book_3"

    storage.save_image_bytes_atomic(paths["raw"], raw_bytes)
    assert paths["raw"].read_bytes() == raw_bytes

    # Construct wide image (200x100) tagged with EXIF Orientation=6 (rotate 90 CW -> 100x200)
    im = Image.new("RGB", (200, 100), color=(240, 240, 240))
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif.tobytes())
    bgr = decode_image_with_exif(buf.getvalue())
    assert bgr.shape[0] == 200 and bgr.shape[1] == 100


def test_wal_crash_recovery_and_sha_dedup(tmp_path):
    db = Database(tmp_path / "test.db")
    try:
        book = db.create_book(title="测试恢复书目")
        p1 = db.insert_page(book_id=book["id"], sha256="aa" * 32, status="CLEANING")
        p2 = db.insert_page(book_id=book["id"], sha256="bb" * 32, status="OCR_RUNNING")
        p3 = db.insert_page(book_id=book["id"], sha256="cc" * 32, status="LLM_RUNNING")

        assert db.has_sha256(book["id"], "aa" * 32) is True
        assert db.has_sha256(book["id"], "ff" * 32) is False

        rec = db.recover_stale_locks()
        assert rec["CLEANING"] == 1
        assert rec["OCR_RUNNING"] == 1
        assert rec["LLM_RUNNING"] == 1

        assert db.get_page(p1["id"])["status"] == "CAPTURED"
        assert db.get_page(p2["id"])["status"] == "CLEANED"
        assert db.get_page(p3["id"])["status"] == "OCR_DONE"
    finally:
        db.close()


def test_sqlite_10k_row_scale_benchmark(tmp_path):
    db = Database(tmp_path / "scale_10k.db")
    try:
        book = db.create_book(title="十万页性能基准测试册")
        now = utc_now_iso()
        rows = []
        for i in range(1, 10001):
            status = "NEEDS_REVIEW" if i % 20 == 0 else "READY"
            flags = '["low_ocr_confidence"]' if i % 20 == 0 else "[]"
            rows.append(
                (
                    book["id"],
                    i,
                    "S",
                    "import_folder",
                    f"page_{i:05d}.jpg",
                    f"{i:064x}",
                    f"{i:016x}",
                    f"storage/book_1/{i % 256:02x}/{i}_raw.jpg",
                    f"storage/book_1/{i % 256:02x}/{i}_clean.png",
                    f"storage/book_1/{i % 256:02x}/{i}_thumb.jpg",
                    status,
                    140.0,
                    210.0,
                    0,
                    0.94 if i % 20 else 0.72,
                    f"第{i}页测试正文内容",
                    f"第{i}页测试正文内容",
                    "[]",
                    flags,
                    "{}",
                    "OFF",
                    "",
                    0,
                    "",
                    "",
                    now,
                    now,
                )
            )

        conn = db._get_conn()
        with db._write_lock:
            conn.execute("BEGIN IMMEDIATE;")
            conn.executemany(
                """
                INSERT INTO pages(
                    book_id, seq_no, spread_side, source_type, source_ref,
                    sha256, dhash, raw_path, clean_path, thumb_path, status,
                    blur_score, brightness, is_duplicate, ocr_confidence,
                    ocr_text, clean_text, ocr_blocks_json, qa_flags_json,
                    clean_meta_json, llm_status, error_msg, retry_count,
                    locked_by, locked_at, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
            conn.execute("COMMIT;")

        t0 = time.perf_counter()
        stats = db.get_book_stats(book["id"])
        stats_ms = (time.perf_counter() - t0) * 1000.0

        t1 = time.perf_counter()
        items, total = db.list_pages(book["id"], status_filter="ALL", limit=40, offset=5000)
        query_ms = (time.perf_counter() - t1) * 1000.0

        assert stats["total"] == 10000
        assert stats["needs_review"] == 500
        assert total == 10000
        assert len(items) == 40
        assert items[0]["seq_no"] == 5001
        assert stats_ms < 100.0, f"stats aggregation took {stats_ms:.1f}ms"
        assert query_ms < 100.0, f"paginated query took {query_ms:.1f}ms"
    finally:
        db.close()
