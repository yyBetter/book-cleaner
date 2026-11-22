"""Resumable background worker pools, SSE event bus, bulk folder importer, and export runner."""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cv2
import pymupdf

from book_cleaner.camera import CameraManager
from book_cleaner.config import AppConfig
from book_cleaner.db import Database
from book_cleaner.pipeline.data_clean import extract_book_structured_data
from book_cleaner.pipeline.deepseek_addon import DeepSeekAddonManager
from book_cleaner.pipeline.excel_exporter import export_excel_workbook
from book_cleaner.pipeline.exporter import (
    export_epub,
    export_jsonl,
    export_markdown,
    export_searchable_pdf,
)
from book_cleaner.pipeline.image_clean import CleanProfile, clean_page_image, split_double_spread
from book_cleaner.pipeline.llm_clean import LLMConfig, clean_text_with_llm, should_run_llm
from book_cleaner.pipeline.ocr_router import SmartOCRRouter
from book_cleaner.pipeline.offline_ocr_hub import OfflineOCRHub, is_local_only_url
from book_cleaner.pipeline.pii_redactor import ConsistentPIIRedactor, parse_custom_redact_terms
from book_cleaner.pipeline.qa import evaluate_capture_qa
from book_cleaner.storage import (
    ShardedStorage,
    compute_sha256,
    decode_image_with_exif,
    encode_jpeg,
    encode_png,
    make_thumbnail,
)


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
SUPPORTED_IMPORT_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf"}


def _natural_sort_key(path: Path) -> list[Any]:
    parts = re.split(r"(\d+)", str(path).lower())
    return [int(p) if p.isdigit() else p for p in parts]


class EventBus:
    """Thread-safe pub/sub queue broadcaster for Server-Sent Events (SSE)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[str]] = set()

    def subscribe(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=250)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except queue.Empty:
                    pass


class PipelineCoordinator:
    """Coordinates instant capture ingestion, L1/L2/L3 workers, bulk imports, and exports."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        storage: ShardedStorage,
        *,
        ocr_engine: Any | None = None,
        camera: CameraManager | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.storage = storage
        self.events = EventBus()
        self.deepseek_addon = DeepSeekAddonManager(config.data_dir, events=self.events)
        self.offline_ocr_hub = OfflineOCRHub(
            config.data_dir, deepseek_addon=self.deepseek_addon, events=self.events
        )
        self.ocr_engine = (
            ocr_engine
            if ocr_engine is not None
            else SmartOCRRouter(
                db=db,
                addon_manager=self.deepseek_addon,
                offline_hub=self.offline_ocr_hub,
            )
        )
        self.camera = camera if camera is not None else CameraManager()

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # cleared = running, set = paused
        self._clean_wake = threading.Event()
        self._ocr_wake = threading.Event()
        self._llm_wake = threading.Event()

        self._started = False
        self._threads: list[threading.Thread] = []
        self._clean_pool: ThreadPoolExecutor | None = None
        self._ocr_pool: ThreadPoolExecutor | None = None
        self._llm_pool: ThreadPoolExecutor | None = None
        self._job_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job-worker")

        self.camera.set_auto_capture_callback(self._handle_auto_camera_frame)

    def _handle_auto_camera_frame(self, book_id: int, jpeg_bytes: bytes) -> None:
        try:
            self.ingest_image_bytes(
                book_id=book_id,
                raw_bytes=jpeg_bytes,
                source_type="camera_auto",
                source_ref="auto_trigger",
            )
        except Exception:
            pass

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def pause(self) -> None:
        self._pause_event.set()
        self.events.publish("pipeline_state", {"paused": True})

    def resume(self) -> None:
        self._pause_event.clear()
        self._clean_wake.set()
        self._ocr_wake.set()
        self._llm_wake.set()
        self.events.publish("pipeline_state", {"paused": False})

    def start(self) -> None:
        if self._started:
            return
        self.db.recover_stale_locks()
        self._stop_event.clear()
        self._clean_pool = ThreadPoolExecutor(
            max_workers=max(1, self.config.clean_workers), thread_name_prefix="clean"
        )
        self._ocr_pool = ThreadPoolExecutor(
            max_workers=max(1, self.config.ocr_workers), thread_name_prefix="ocr"
        )
        self._llm_pool = ThreadPoolExecutor(
            max_workers=max(1, self.config.llm_workers), thread_name_prefix="llm"
        )

        specs = [
            ("dispatcher-clean", self._clean_loop, self._clean_wake),
            ("dispatcher-ocr", self._ocr_loop, self._ocr_wake),
            ("dispatcher-llm", self._llm_loop, self._llm_wake),
        ]
        self._threads = []
        for name, target, wake in specs:
            wake.set()
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        self._clean_wake.set()
        self._ocr_wake.set()
        self._llm_wake.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads.clear()
        for pool in (self._clean_pool, self._ocr_pool, self._llm_pool):
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        self._clean_pool = None
        self._ocr_pool = None
        self._llm_pool = None
        self._started = False

    def wake_all(self) -> None:
        self._clean_wake.set()
        self._ocr_wake.set()
        self._llm_wake.set()

    # ------------------------------------------------------------------
    # Instant Capture Ingestion (decoupled from OCR latency)
    # ------------------------------------------------------------------
    def ingest_image_bytes(
        self,
        *,
        book_id: int,
        raw_bytes: bytes,
        source_type: str = "upload",
        source_ref: str = "",
        profile_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        book = self.db.get_book(book_id)
        if book is None:
            raise KeyError(f"Book {book_id} not found")

        bgr = decode_image_with_exif(raw_bytes)
        merged_profile = {**book["profile"], **(profile_override or {})}
        prof = CleanProfile.from_dict(merged_profile)

        pieces = split_double_spread(bgr, split_spread=prof.split_spread)
        recent_hashes = self.db.get_recent_dhashes(book_id, limit=5)

        created_pages: list[dict[str, Any]] = []
        qa_summaries: list[dict[str, Any]] = []

        for side, piece_bgr in pieces:
            piece_bytes = encode_jpeg(piece_bgr, quality=94)
            sha256 = compute_sha256(piece_bytes)

            qa = evaluate_capture_qa(
                piece_bgr,
                recent_dhashes=recent_hashes,
                blur_threshold=self.config.blur_warn_threshold,
                dup_threshold=self.config.dup_hamming_threshold,
                dark_threshold=self.config.dark_threshold,
                overexposed_threshold=self.config.overexposed_threshold,
            )
            recent_hashes.insert(0, qa.dhash)
            recent_hashes = recent_hashes[:5]

            thumb_bgr = make_thumbnail(piece_bgr, max_width=420)
            thumb_bytes = encode_jpeg(thumb_bgr, quality=85)

            clean_meta: dict[str, Any] = {}
            if profile_override:
                clean_meta["profile_override"] = profile_override

            # Insert in STAGING first so files exist atomically before worker claims CAPTURED.
            staged = self.db.insert_page(
                book_id=book_id,
                spread_side=side,
                source_type=source_type,
                source_ref=source_ref,
                sha256=sha256,
                dhash=qa.dhash,
                status="STAGING",
                blur_score=qa.blur_score,
                brightness=qa.brightness,
                is_duplicate=qa.is_duplicate,
                qa_flags=qa.flags,
                clean_meta=clean_meta,
            )
            paths = self.storage.page_paths(book_id, staged["id"], sha256)
            self.storage.save_image_bytes_atomic(paths["raw"], piece_bytes)
            self.storage.save_image_bytes_atomic(paths["thumb"], thumb_bytes)

            updated = self.db.update_page(
                staged["id"],
                raw_path=self.storage.to_rel(paths["raw"]),
                thumb_path=self.storage.to_rel(paths["thumb"]),
                status="CAPTURED",
            )
            assert updated is not None
            created_pages.append(updated)
            qa_summaries.append(qa.to_dict())
            self.events.publish(
                "page_captured",
                {
                    "book_id": book_id,
                    "page_id": updated["id"],
                    "seq_no": updated["seq_no"],
                    "spread_side": side,
                    "qa": qa.to_dict(),
                },
            )

        self._clean_wake.set()
        return {
            "pages": created_pages,
            "qa": qa_summaries[0] if qa_summaries else {},
            "split_count": len(created_pages),
        }

    # ------------------------------------------------------------------
    # Worker Dispatchers & Single-Stage Processors
    # ------------------------------------------------------------------
    def _clean_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                self._clean_wake.wait(timeout=0.5)
                self._clean_wake.clear()
                continue
            batch = self.db.claim_next_pages(
                "CAPTURED",
                "CLEANING",
                worker_id="clean-worker",
                limit=max(1, self.config.clean_workers),
            )
            if not batch:
                self._clean_wake.wait(timeout=0.4)
                self._clean_wake.clear()
                continue
            assert self._clean_pool is not None
            futures = [self._clean_pool.submit(self.process_clean_page, p) for p in batch]
            for fut in futures:
                try:
                    fut.result()
                except Exception:
                    pass

    def _ocr_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                self._ocr_wake.wait(timeout=0.5)
                self._ocr_wake.clear()
                continue
            batch = self.db.claim_next_pages(
                "CLEANED",
                "OCR_RUNNING",
                worker_id="ocr-worker",
                limit=max(1, self.config.ocr_workers),
            )
            if not batch:
                self._ocr_wake.wait(timeout=0.4)
                self._ocr_wake.clear()
                continue
            assert self._ocr_pool is not None
            futures = [self._ocr_pool.submit(self.process_ocr_page, p) for p in batch]
            for fut in futures:
                try:
                    fut.result()
                except Exception:
                    pass

    def _llm_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                self._llm_wake.wait(timeout=0.5)
                self._llm_wake.clear()
                continue
            batch = self.db.claim_next_pages(
                "OCR_DONE",
                "LLM_RUNNING",
                worker_id="llm-worker",
                limit=max(1, self.config.llm_workers),
            )
            if not batch:
                self._llm_wake.wait(timeout=0.5)
                self._llm_wake.clear()
                continue
            assert self._llm_pool is not None
            futures = [self._llm_pool.submit(self.process_llm_page, p) for p in batch]
            for fut in futures:
                try:
                    fut.result()
                except Exception:
                    pass

    def process_clean_page(self, page: dict[str, Any]) -> dict[str, Any]:
        page_id = int(page["id"])
        try:
            book = self.db.get_book(int(page["book_id"]))
            base_prof = book["profile"] if book else {}
            override = (page.get("clean_meta") or {}).get("profile_override") or {}
            profile = CleanProfile.from_dict({**base_prof, **override})

            raw_file = self.storage.resolve_path(page["raw_path"])
            raw_bytes = raw_file.read_bytes()
            bgr = decode_image_with_exif(raw_bytes)
            result = clean_page_image(bgr, profile)

            paths = self.storage.page_paths(int(page["book_id"]), page_id, page["sha256"])
            clean_png = encode_png(result.image)
            thumb_jpg = encode_jpeg(make_thumbnail(result.image, max_width=420), quality=85)
            self.storage.save_image_bytes_atomic(paths["clean"], clean_png)
            self.storage.save_image_bytes_atomic(paths["thumb"], thumb_jpg)

            merged_meta = {**(page.get("clean_meta") or {}), **result.diagnostics, "profile": profile.to_dict()}
            updated = self.db.update_page(
                page_id,
                clean_path=self.storage.to_rel(paths["clean"]),
                thumb_path=self.storage.to_rel(paths["thumb"]),
                clean_meta=merged_meta,
                status="CLEANED",
                locked_by="",
                locked_at="",
                error_msg="",
            )
            assert updated is not None
            self._ocr_wake.set()
            self.events.publish(
                "page_updated",
                {"book_id": updated["book_id"], "page_id": page_id, "status": "CLEANED"},
            )
            return updated
        except Exception as exc:
            updated = self.db.update_page(
                page_id,
                status="FAILED",
                error_msg=f"图像清洗失败: {exc}",
                retry_count=int(page.get("retry_count", 0)) + 1,
                locked_by="",
                locked_at="",
            )
            self.events.publish(
                "page_updated",
                {"book_id": page["book_id"], "page_id": page_id, "status": "FAILED"},
            )
            return updated or page

    def process_ocr_page(self, page: dict[str, Any]) -> dict[str, Any]:
        page_id = int(page["id"])
        try:
            img_rel = page.get("clean_path") or page.get("raw_path")
            img_path = self.storage.resolve_path(img_rel)
            bgr = decode_image_with_exif(img_path.read_bytes())

            ocr_res = self.ocr_engine.recognize(
                bgr, low_conf_threshold=self.config.ocr_review_threshold
            )

            existing_flags = [
                f for f in (page.get("qa_flags") or []) if f not in ("low_ocr_confidence", "empty_ocr")
            ]
            merged_flags = list(dict.fromkeys([*existing_flags, *ocr_res.flags]))

            settings = self.db.get_settings()
            ocr_cfg = settings.get("ocr") or {}
            llm_cfg = LLMConfig.from_dict(settings.get("llm"))
            strict_off = bool(ocr_cfg.get("strict_offline", True))
            llm_blocked_offline = strict_off and bool(llm_cfg.base_url) and not is_local_only_url(llm_cfg.base_url)
            use_llm = (not llm_blocked_offline) and should_run_llm(
                llm_cfg, ocr_res.avg_confidence, ocr_res.formatted_text
            )

            if use_llm:
                next_status = "OCR_DONE"
                llm_status = "PENDING"
            else:
                next_status = "NEEDS_REVIEW" if merged_flags else "READY"
                llm_status = "SKIPPED" if llm_cfg.mode != "off" else "OFF"

            engine_used = str(getattr(ocr_res, "engine_used", "rapidocr") or "rapidocr")
            upgrade_reasons = list(getattr(ocr_res, "upgrade_reasons", []) or [])
            merged_meta = {
                **(page.get("clean_meta") or {}),
                "ocr_engine_used": engine_used,
                "ocr_upgrade_reasons": upgrade_reasons,
            }

            updated = self.db.update_page(
                page_id,
                ocr_confidence=ocr_res.avg_confidence,
                ocr_text=ocr_res.formatted_text,
                clean_text=ocr_res.formatted_text,
                ocr_blocks=[b.to_dict() for b in ocr_res.blocks],
                clean_meta=merged_meta,
                qa_flags=merged_flags,
                llm_status=llm_status,
                status=next_status,
                locked_by="",
                locked_at="",
                error_msg="",
            )
            assert updated is not None
            if use_llm:
                self._llm_wake.set()
            self.events.publish(
                "page_updated",
                {
                    "book_id": updated["book_id"],
                    "page_id": page_id,
                    "status": next_status,
                    "ocr_confidence": updated["ocr_confidence"],
                    "ocr_engine_used": engine_used,
                },
            )
            return updated
        except Exception as exc:
            updated = self.db.update_page(
                page_id,
                status="FAILED",
                error_msg=f"OCR 识别失败: {exc}",
                retry_count=int(page.get("retry_count", 0)) + 1,
                locked_by="",
                locked_at="",
            )
            self.events.publish(
                "page_updated",
                {"book_id": page["book_id"], "page_id": page_id, "status": "FAILED"},
            )
            return updated or page

    def process_llm_page(self, page: dict[str, Any]) -> dict[str, Any]:
        page_id = int(page["id"])
        settings = self.db.get_settings()
        ocr_cfg = settings.get("ocr") or {}
        llm_cfg = LLMConfig.from_dict(settings.get("llm"))
        source_text = (page.get("clean_text") or page.get("ocr_text") or "").strip()
        qa_flags = page.get("qa_flags") or []

        try:
            strict_off = bool(ocr_cfg.get("strict_offline", True))
            if not source_text or (strict_off and not is_local_only_url(llm_cfg.base_url)):
                next_status = "NEEDS_REVIEW" if qa_flags else "READY"
                updated = self.db.update_page(
                    page_id, status=next_status, llm_status="SKIPPED", locked_by="", locked_at=""
                )
                return updated or page

            cleaned_text, meta = clean_text_with_llm(source_text, llm_cfg)
            next_status = "NEEDS_REVIEW" if qa_flags else "READY"
            merged_meta = {**(page.get("clean_meta") or {}), "llm": meta}
            updated = self.db.update_page(
                page_id,
                clean_text=cleaned_text,
                clean_meta=merged_meta,
                llm_status="DONE",
                status=next_status,
                locked_by="",
                locked_at="",
                error_msg="",
            )
            assert updated is not None
            self.events.publish(
                "page_updated",
                {"book_id": updated["book_id"], "page_id": page_id, "status": next_status, "llm": "DONE"},
            )
            return updated
        except Exception as exc:
            updated = self.db.update_page(
                page_id,
                llm_status="FAILED",
                status="NEEDS_REVIEW",
                error_msg=f"LLM 精校失败(已保留本地OCR结果): {exc}",
                locked_by="",
                locked_at="",
            )
            self.events.publish(
                "page_updated",
                {"book_id": page["book_id"], "page_id": page_id, "status": "NEEDS_REVIEW"},
            )
            return updated or page

    def reclean_page(
        self,
        page_id: int,
        profile_patch: dict[str, Any] | None = None,
        *,
        rerun_ocr: bool = False,
    ) -> dict[str, Any]:
        page = self.db.get_page(page_id)
        if page is None:
            raise KeyError(f"Page {page_id} not found")
        book = self.db.get_book(int(page["book_id"]))
        base_prof = book["profile"] if book else {}
        prev_override = (page.get("clean_meta") or {}).get("profile_override") or {}
        merged_profile = {**base_prof, **prev_override, **(profile_patch or {}), "split_spread": False}
        profile = CleanProfile.from_dict(merged_profile)

        raw_path = self.storage.resolve_path(page["raw_path"])
        bgr = decode_image_with_exif(raw_path.read_bytes())
        result = clean_page_image(bgr, profile)

        paths = self.storage.page_paths(int(page["book_id"]), page_id, page["sha256"])
        self.storage.save_image_bytes_atomic(paths["clean"], encode_png(result.image))
        self.storage.save_image_bytes_atomic(
            paths["thumb"], encode_jpeg(make_thumbnail(result.image, max_width=420), quality=85)
        )

        merged_meta = {
            **(page.get("clean_meta") or {}),
            **result.diagnostics,
            "profile_override": {**prev_override, **(profile_patch or {})},
            "profile": profile.to_dict(),
        }
        next_status = "CLEANED" if rerun_ocr else page["status"]
        updated = self.db.update_page(
            page_id,
            clean_path=self.storage.to_rel(paths["clean"]),
            thumb_path=self.storage.to_rel(paths["thumb"]),
            clean_meta=merged_meta,
            status=next_status,
        )
        assert updated is not None
        if rerun_ocr:
            self._ocr_wake.set()
        self.events.publish(
            "page_updated",
            {"book_id": updated["book_id"], "page_id": page_id, "status": updated["status"]},
        )
        return updated

    def run_single_llm_clean(self, page_id: int) -> dict[str, Any]:
        page = self.db.get_page(page_id)
        if page is None:
            raise KeyError(f"Page {page_id} not found")
        settings = self.db.get_settings()
        llm_cfg = LLMConfig.from_dict(settings.get("llm"))
        source_text = (page.get("clean_text") or page.get("ocr_text") or "").strip()
        if not source_text:
            raise ValueError("该页暂无可校对的文本")
        cleaned_text, meta = clean_text_with_llm(source_text, llm_cfg)
        merged_meta = {**(page.get("clean_meta") or {}), "llm": meta}
        updated = self.db.update_page(
            page_id,
            clean_text=cleaned_text,
            clean_meta=merged_meta,
            llm_status="DONE",
        )
        assert updated is not None
        self.events.publish(
            "page_updated",
            {"book_id": updated["book_id"], "page_id": page_id, "status": updated["status"], "llm": "DONE"},
        )
        return updated

    # ------------------------------------------------------------------
    # Bulk Folder / PDF Import & Exports
    # ------------------------------------------------------------------
    def import_folder_sync(
        self,
        *,
        book_id: int,
        folder_path: str | Path,
        recursive: bool = True,
        profile_override: dict[str, Any] | None = None,
        job_id: int | None = None,
    ) -> dict[str, Any]:
        folder = Path(folder_path).expanduser().resolve()
        if not folder.exists() or not folder.is_dir():
            raise FileNotFoundError(f"目录不存在: {folder}")

        iterator = folder.rglob("*") if recursive else folder.glob("*")
        files = sorted(
            [p for p in iterator if p.is_file() and p.suffix.lower() in SUPPORTED_IMPORT_EXTENSIONS],
            key=_natural_sort_key,
        )

        if job_id is not None:
            self.db.update_job(job_id, status="RUNNING", total_items=len(files), done_items=0, failed_items=0)
            self.events.publish("job_progress", {"job_id": job_id, "status": "RUNNING", "total": len(files), "done": 0})

        done = 0
        failed = 0
        skipped_dup = 0
        ingested_pages = 0

        for path in files:
            try:
                if path.suffix.lower() == ".pdf":
                    doc = pymupdf.open(str(path))
                    try:
                        for page_idx in range(len(doc)):
                            pix = doc[page_idx].get_pixmap(dpi=200)
                            png_bytes = pix.tobytes("png")
                            res = self.ingest_image_bytes(
                                book_id=book_id,
                                raw_bytes=png_bytes,
                                source_type="import_pdf",
                                source_ref=f"{path.name}#p{page_idx + 1}",
                                profile_override=profile_override,
                            )
                            ingested_pages += len(res["pages"])
                    finally:
                        doc.close()
                else:
                    raw_bytes = path.read_bytes()
                    file_sha = compute_sha256(raw_bytes)
                    if self.db.has_sha256(book_id, file_sha):
                        skipped_dup += 1
                    else:
                        res = self.ingest_image_bytes(
                            book_id=book_id,
                            raw_bytes=raw_bytes,
                            source_type="import_folder",
                            source_ref=path.name,
                            profile_override=profile_override,
                        )
                        ingested_pages += len(res["pages"])
                done += 1
            except Exception:
                failed += 1

            if job_id is not None and (done + failed) % 5 == 0 or (done + failed) == len(files):
                if job_id is not None:
                    self.db.update_job(job_id, done_items=done, failed_items=failed)
                    self.events.publish(
                        "job_progress",
                        {
                            "job_id": job_id,
                            "status": "RUNNING",
                            "total": len(files),
                            "done": done,
                            "failed": failed,
                        },
                    )

        summary = {
            "files_found": len(files),
            "done_files": done,
            "failed_files": failed,
            "skipped_duplicates": skipped_dup,
            "ingested_pages": ingested_pages,
        }
        if job_id is not None:
            self.db.update_job(
                job_id,
                status="DONE",
                done_items=done,
                failed_items=failed,
                result_path=str(folder),
            )
            self.events.publish("job_progress", {"job_id": job_id, "status": "DONE", **summary})
        return summary

    def start_folder_import_job(
        self,
        *,
        book_id: int,
        folder_path: str,
        recursive: bool = True,
        profile_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        folder = Path(folder_path).expanduser().resolve()
        if not folder.exists() or not folder.is_dir():
            raise FileNotFoundError(f"目录不存在: {folder}")
        job = self.db.create_job(
            job_type="IMPORT_FOLDER",
            book_id=book_id,
            params={"folder_path": str(folder), "recursive": recursive},
        )

        def _runner() -> None:
            try:
                self.import_folder_sync(
                    book_id=book_id,
                    folder_path=folder,
                    recursive=recursive,
                    profile_override=profile_override,
                    job_id=job["id"],
                )
            except Exception as exc:
                self.db.update_job(job["id"], status="FAILED", error_msg=str(exc))
                self.events.publish("job_progress", {"job_id": job["id"], "status": "FAILED", "error": str(exc)})

        self._job_pool.submit(_runner)
        return job

    def run_data_cleaning_sync(
        self,
        *,
        book_id: int,
        template_id: int | None = None,
    ) -> dict[str, Any]:
        book = self.db.get_book(book_id)
        if book is None:
            raise KeyError(f"Book {book_id} not found")
        template = (
            self.db.get_excel_template(template_id)
            if template_id is not None
            else self.db.get_default_excel_template()
        )
        if template is None:
            raise KeyError(f"Excel template {template_id} not found")

        pages = self.db.list_export_pages(book_id)
        rows_to_store, summary = extract_book_structured_data(book, pages, template)
        self.db.replace_structured_rows(book_id, template["id"], rows_to_store)
        self.events.publish("data_cleaned", {"book_id": book_id, "summary": summary})
        return summary

    def run_export_sync(
        self,
        *,
        book_id: int,
        fmt: str,
        out_path: Path | None = None,
        template_id: int | None = None,
        redact_pii: bool | None = None,
        custom_redact_terms: list[str] | str | None = None,
    ) -> Path:
        book = self.db.get_book(book_id)
        if book is None:
            raise KeyError(f"Book {book_id} not found")
        pages = self.db.list_export_pages(book_id)
        fmt_norm = fmt.lower().strip()
        ext_map = {
            "pdf": ".pdf",
            "markdown": ".md",
            "md": ".md",
            "epub": ".epub",
            "jsonl": ".jsonl",
            "xlsx": ".xlsx",
            "excel": ".xlsx",
        }
        if fmt_norm not in ext_map:
            raise ValueError(f"Unsupported export format: {fmt}")

        if out_path is None:
            safe_title = re.sub(r'[\\/:*?"<>|\s]+', "_", str(book["title"])).strip("_") or f"book_{book_id}"
            ts = time.strftime("%Y%m%d_%H%M%S")
            out_path = self.storage.export_dir(book_id) / f"{safe_title}_{ts}{ext_map[fmt_norm]}"

        template = (
            self.db.get_excel_template(template_id)
            if template_id is not None
            else self.db.get_default_excel_template()
        )
        rules_dict = (template.get("rules") if template else {}) or {}
        eff_redact = bool(rules_dict.get("redact_pii", False)) if redact_pii is None else bool(redact_pii)
        eff_terms = parse_custom_redact_terms(
            rules_dict.get("custom_redact_terms") if custom_redact_terms is None else custom_redact_terms
        )
        redactor = ConsistentPIIRedactor(enabled=True, custom_terms=eff_terms) if eff_redact else None

        if fmt_norm in ("xlsx", "excel"):
            assert template is not None
            summary = self.run_data_cleaning_sync(book_id=book_id, template_id=template["id"])
            structured_rows = self.db.list_all_structured_rows(book_id)
            page_assignments = self.db.list_page_assignments(book_id, int(template["id"]))
            return export_excel_workbook(
                book,
                template,
                structured_rows,
                summary,
                out_path,
                pages=pages,
                page_assignments=page_assignments,
                redact_pii=eff_redact,
                custom_redact_terms=eff_terms,
            )

        if fmt_norm == "pdf":
            return export_searchable_pdf(book, pages, out_path, self.storage)
        if fmt_norm in ("markdown", "md"):
            return export_markdown(book, pages, out_path, redactor=redactor)
        if fmt_norm == "epub":
            return export_epub(book, pages, out_path)
        return export_jsonl(book, pages, out_path, redactor=redactor)

    def start_export_job(
        self,
        *,
        book_id: int,
        fmt: str,
        template_id: int | None = None,
        redact_pii: bool | None = None,
        custom_redact_terms: list[str] | str | None = None,
    ) -> dict[str, Any]:
        book = self.db.get_book(book_id)
        if book is None:
            raise KeyError(f"Book {book_id} not found")
        pages = self.db.list_export_pages(book_id)
        parsed_terms = parse_custom_redact_terms(custom_redact_terms) if custom_redact_terms is not None else None
        job = self.db.create_job(
            job_type=f"EXPORT_{fmt.upper()}",
            book_id=book_id,
            total_items=len(pages),
            params={
                "format": fmt,
                "template_id": template_id,
                "redact_pii": redact_pii,
                "custom_redact_terms": parsed_terms or [],
            },
        )

        def _runner() -> None:
            try:
                self.db.update_job(job["id"], status="RUNNING")
                out = self.run_export_sync(
                    book_id=book_id,
                    fmt=fmt,
                    template_id=template_id,
                    redact_pii=redact_pii,
                    custom_redact_terms=parsed_terms,
                )
                self.db.update_job(
                    job["id"],
                    status="DONE",
                    done_items=len(pages),
                    result_path=self.storage.to_rel(out),
                )
                self.events.publish(
                    "job_progress",
                    {
                        "job_id": job["id"],
                        "status": "DONE",
                        "book_id": book_id,
                        "format": fmt,
                        "result_path": self.storage.to_rel(out),
                    },
                )
            except Exception as exc:
                self.db.update_job(job["id"], status="FAILED", error_msg=str(exc))
                self.events.publish("job_progress", {"job_id": job["id"], "status": "FAILED", "error": str(exc)})

        self._job_pool.submit(_runner)
        return job
