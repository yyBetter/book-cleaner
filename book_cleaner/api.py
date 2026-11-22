"""FastAPI application server, SSE stream, LAN mobile pairing, and static workbench routes."""

from __future__ import annotations

import io
import ipaddress
import queue
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import segno
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from openpyxl import load_workbook

from book_cleaner import __version__
from book_cleaner.android_adb import get_adb_status, setup_adb_reverse
from book_cleaner.config import AppConfig
from book_cleaner.db import CATBOOST_BATCH_RULES, Database
from book_cleaner.demo_generator import generate_demo_pages
from book_cleaner.pipeline.deepseek_addon import DEFAULT_HF_REPO
from book_cleaner.pipeline.excel_exporter import read_headers_from_xlsx, read_workbook_sheets_from_xlsx
from book_cleaner.pipeline.llm_clean import (
    LLMConfig,
    extract_sheet_rows_with_llm,
    test_llm_connection,
)
from book_cleaner.pipeline.ocr_engine import reformat_page_from_blocks
from book_cleaner.pipeline.ocr_router import OCRSettings, test_deepseek_api_connection
from book_cleaner.pipeline.offline_ocr_hub import is_local_only_url
from book_cleaner.pipeline.sheet_matcher import match_book_to_template_pack
from book_cleaner.storage import ShardedStorage
from book_cleaner.worker import PipelineCoordinator


WEB_DIR = Path(__file__).resolve().parent / "web"


# ----------------------------------------------------------------------
# Pydantic Request Models
# ----------------------------------------------------------------------
class BookCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    author: str = ""
    isbn: str = ""
    profile: dict[str, Any] | None = None


class BookUpdate(BaseModel):
    title: str | None = None
    author: str | None = None
    isbn: str | None = None
    profile: dict[str, Any] | None = None


class PageUpdate(BaseModel):
    clean_text: str | None = None
    status: str | None = None
    seq_no: int | None = None


class BatchDeletePagesRequest(BaseModel):
    page_ids: list[int] = Field(default_factory=list)


class RecleanRequest(BaseModel):
    profile_patch: dict[str, Any] = Field(default_factory=dict)
    rerun_ocr: bool = False


class RotatePageRequest(BaseModel):
    delta_deg: int = Field(default=90)
    rerun_ocr: bool = True


class ImportFolderRequest(BaseModel):
    folder_path: str
    recursive: bool = True
    profile_override: dict[str, Any] | None = None


class ExportRequest(BaseModel):
    format: str = "pdf"
    template_id: int | None = None


class ExcelTemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = ""
    entry_sheet_name: str = "正文结构化条目"
    table_sheet_name: str = "提取表格明细"
    rules: dict[str, Any] | None = None
    columns: list[dict[str, Any]] | None = None
    sheets: list[dict[str, Any]] | None = None


class ExcelTemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    entry_sheet_name: str | None = None
    table_sheet_name: str | None = None
    rules: dict[str, Any] | None = None
    columns: list[dict[str, Any]] | None = None
    sheets: list[dict[str, Any]] | None = None


class PageAssignmentRequest(BaseModel):
    template_id: int | None = None
    page_id: int
    sheet_id: str


class LearnAliasRequest(BaseModel):
    sheet_id: str
    field_key: str
    source_alias: str


class LLMSheetExtractRequest(BaseModel):
    template_id: int | None = None
    sheet_id: str


class DataCleanRunRequest(BaseModel):
    template_id: int | None = None


class CameraOpenRequest(BaseModel):
    index: int = 0
    width: int = 1920
    height: int = 1080


class AutoTriggerRequest(BaseModel):
    enabled: bool
    book_id: int | None = None
    settle_ms: int | None = None
    hands_on_assist: bool = True
    margin_ratio: float = 0.16


class AdbReverseRequest(BaseModel):
    book_id: int | None = None
    open_on_phone: bool = True


class SettingsUpdateRequest(BaseModel):
    ocr: dict[str, Any] | None = None
    llm: dict[str, Any] | None = None
    capture_profile: dict[str, Any] | None = None


class AddonInstallRequest(BaseModel):
    use_cn_mirror: bool = True
    repo_id: str = DEFAULT_HF_REPO
    force: bool = False


class ModelDownloadRequest(BaseModel):
    use_cn_mirror: bool = True
    force: bool = False


class OCRTestRequest(BaseModel):
    api_base_url: str = "https://api.siliconflow.cn/v1"
    api_key: str = ""
    api_model: str = "deepseek-ai/DeepSeek-OCR-2"
    strict_offline: bool | None = None


class LLMTestRequest(BaseModel):
    mode: str = "auto_low_conf"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    strict_offline: bool | None = None


# ----------------------------------------------------------------------
# LAN Helpers
# ----------------------------------------------------------------------
def _score_lan_candidate(iface: str, ip_str: str) -> int | None:
    """Rank candidate IPv4 addresses so real Wi-Fi/Ethernet LAN wins over VPN/TUN virtual NICs."""
    try:
        addr = ipaddress.IPv4Address(ip_str)
    except ipaddress.AddressValueError:
        return None
    if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return None
    # Skip VPN/TUN benchmark range (198.18.0.0/15) & CGNAT (100.64.0.0/10)
    if addr in ipaddress.IPv4Network("198.18.0.0/15") or addr in ipaddress.IPv4Network("100.64.0.0/10"):
        return None

    low_iface = iface.lower()
    if any(
        p in low_iface
        for p in (
            "utun",
            "tun",
            "tap",
            "ppp",
            "awdl",
            "llw",
            "docker",
            "veth",
            "vethernet",
            "wsl",
            "hyper-v",
            "vmware",
            "virtualbox",
            "wintun",
            "clash",
            "mihomo",
            "tailscale",
            "zerotier",
            "npcap",
            "isatap",
            "teredo",
            "loopback",
        )
    ):
        return None

    score = 10
    if addr in ipaddress.IPv4Network("192.168.0.0/16"):
        score += 40
    elif addr in ipaddress.IPv4Network("10.0.0.0/8"):
        score += 30
    elif addr in ipaddress.IPv4Network("172.16.0.0/12"):
        score += 25
    if low_iface in ("en0", "eth0", "wlan0") or low_iface.startswith(
        ("en", "eth", "wlan", "wi-fi", "wifi", "以太网", "ethernet", "本地连接")
    ):
        score += 15
    return score


def detect_lan_ip() -> str:
    """Detect active LAN IPv4 address for phone QR pairing, avoiding VPN/TUN virtual adapters."""
    candidates: list[tuple[int, str]] = []
    try:
        import psutil

        for iface, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET:
                    score = _score_lan_candidate(iface, a.address)
                    if score is not None:
                        candidates.append((score, str(a.address)))
    except Exception:
        pass

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
        if ip and _score_lan_candidate("socket", ip) is not None:
            return str(ip)
    except OSError:
        pass
    finally:
        sock.close()

    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("127."):
            return str(ip)
    except OSError:
        pass
    return "127.0.0.1"


def make_qr_svg(url: str) -> str:
    qr = segno.make(url, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=5, border=2, xmldecl=False)
    return buf.getvalue().decode("utf-8")


# ----------------------------------------------------------------------
# App Factory
# ----------------------------------------------------------------------
def create_app(
    config: AppConfig | None = None,
    *,
    start_workers: bool = True,
    ocr_engine: Any | None = None,
) -> FastAPI:
    cfg = config or AppConfig()
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    storage = ShardedStorage(cfg.data_dir)
    coordinator = PipelineCoordinator(cfg, db, storage, ocr_engine=ocr_engine)
    db.ensure_default_book()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_workers:
            coordinator.start()
        try:
            yield
        finally:
            coordinator.camera.close()
            coordinator.stop()
            db.close()

    app = FastAPI(title="Book Cleaner Workbench", version=__version__, lifespan=lifespan)
    app.state.config = cfg
    app.state.db = db
    app.state.storage = storage
    app.state.coordinator = coordinator

    static_dir = WEB_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ------------------------------------------------------------------
    # Static UI Entrypoints
    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index_page() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/mobile", response_class=HTMLResponse)
    def mobile_page() -> FileResponse:
        return FileResponse(WEB_DIR / "mobile.html")

    # ------------------------------------------------------------------
    # Health, Stats & Live SSE
    # ------------------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "paused": coordinator.is_paused,
            "data_dir": str(cfg.data_dir),
        }

    @app.get("/api/stats")
    def stats(book_id: int | None = None) -> dict[str, Any]:
        return {
            "global": db.get_global_stats(),
            "book": db.get_book_stats(book_id) if book_id is not None else None,
            "paused": coordinator.is_paused,
            "camera": coordinator.camera.status(),
        }

    @app.get("/api/events")
    def sse_events() -> StreamingResponse:
        sub_q = coordinator.events.subscribe()

        def stream():
            try:
                yield "event: connected\ndata: {}\n\n"
                while True:
                    try:
                        msg = sub_q.get(timeout=15.0)
                        yield f"data: {msg}\n\n"
                    except queue.Empty:
                        yield ": ping\n\n"
            finally:
                coordinator.events.unsubscribe(sub_q)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------
    # Books CRUD
    # ------------------------------------------------------------------
    @app.get("/api/books")
    def list_books() -> dict[str, Any]:
        return {"items": db.list_books()}

    @app.post("/api/books", status_code=201)
    def create_book(body: BookCreate) -> dict[str, Any]:
        book = db.create_book(
            title=body.title, author=body.author, isbn=body.isbn, profile=body.profile
        )
        book["stats"] = db.get_book_stats(book["id"])
        return book

    @app.get("/api/books/{book_id}")
    def get_book(book_id: int) -> dict[str, Any]:
        book = db.get_book(book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        book["stats"] = db.get_book_stats(book_id)
        return book

    @app.patch("/api/books/{book_id}")
    def update_book(book_id: int, body: BookUpdate) -> dict[str, Any]:
        book = db.update_book(
            book_id,
            title=body.title,
            author=body.author,
            isbn=body.isbn,
            profile=body.profile,
        )
        if book is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        book["stats"] = db.get_book_stats(book_id)
        return book

    @app.delete("/api/books/{book_id}")
    def delete_book(book_id: int) -> dict[str, Any]:
        pages = db.list_export_pages(book_id)
        ok = db.delete_book(book_id)
        if not ok:
            raise HTTPException(status_code=404, detail="书目不存在")
        for p in pages:
            storage.delete_file_quietly(p.get("raw_path", ""))
            storage.delete_file_quietly(p.get("clean_path", ""))
            storage.delete_file_quietly(p.get("thumb_path", ""))
        fallback = db.ensure_default_book()
        return {"deleted": True, "book_id": book_id, "fallback_book_id": fallback["id"]}

    # ------------------------------------------------------------------
    # Capture, Bulk Import & Demo Seed
    # ------------------------------------------------------------------
    @app.post("/api/books/{book_id}/capture")
    async def capture_upload(
        book_id: int,
        file: UploadFile = File(...),
        source_type: str = Form("upload"),
        split_spread: str | None = Form(None),
        auto_crop: str | None = Form(None),
        dewarp: str | None = Form(None),
        remove_fingers: str | None = Form(None),
        deskew: str | None = Form(None),
        auto_rotate: str | None = Form(None),
        denoise: str | None = Form(None),
        enhance_mode: str | None = Form(None),
    ) -> dict[str, Any]:
        if db.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        raw_bytes = await file.read()
        if not raw_bytes:
            raise HTTPException(status_code=400, detail="上传图片为空")

        override: dict[str, Any] = {}
        for k, val in (
            ("split_spread", split_spread),
            ("auto_crop", auto_crop),
            ("dewarp", dewarp),
            ("remove_fingers", remove_fingers),
            ("deskew", deskew),
            ("auto_rotate", auto_rotate),
            ("denoise", denoise),
        ):
            if val is not None and val != "":
                override[k] = val.lower() in ("1", "true", "yes", "on")
        if enhance_mode:
            override["enhance_mode"] = enhance_mode

        try:
            return coordinator.ingest_image_bytes(
                book_id=book_id,
                raw_bytes=raw_bytes,
                source_type=source_type,
                source_ref=file.filename or "capture.jpg",
                profile_override=override or None,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"图像解析失败: {exc}") from exc

    @app.post("/api/books/{book_id}/capture-camera")
    def capture_from_camera(book_id: int, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if db.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        try:
            raw_bytes = coordinator.camera.capture_full_frame()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        override = (body or {}).get("profile_override")
        return coordinator.ingest_image_bytes(
            book_id=book_id,
            raw_bytes=raw_bytes,
            source_type="camera",
            source_ref="backend_camera",
            profile_override=override,
        )

    @app.post("/api/books/{book_id}/import-folder")
    def import_folder(book_id: int, body: ImportFolderRequest) -> dict[str, Any]:
        if db.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        try:
            job = coordinator.start_folder_import_job(
                book_id=book_id,
                folder_path=body.folder_path,
                recursive=body.recursive,
                profile_override=body.profile_override,
            )
            return {"job": job}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/books/{book_id}/seed-demo")
    def seed_demo_book(book_id: int) -> dict[str, Any]:
        if db.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        samples = generate_demo_pages()
        created_count = 0
        for fname, raw_bytes in samples:
            override = {"split_spread": True} if "double_spread" in fname else None
            res = coordinator.ingest_image_bytes(
                book_id=book_id,
                raw_bytes=raw_bytes,
                source_type="demo_seed",
                source_ref=fname,
                profile_override=override,
            )
            created_count += len(res["pages"])
        return {"seeded_files": len(samples), "created_pages": created_count}

    # ------------------------------------------------------------------
    # Pages Query, Inspection, Re-clean & Review
    # ------------------------------------------------------------------
    @app.get("/api/books/{book_id}/pages")
    def list_pages(
        book_id: int,
        status: str = Query("ALL"),
        qa: str = Query("ALL"),
        search: str = Query(""),
        order: str = Query("asc"),
        limit: int = Query(40, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        if db.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        items, total = db.list_pages(
            book_id,
            status_filter=status,
            qa_filter=qa,
            search=search or None,
            order=order,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def _cleanup_deleted_pages(book_id: int, deleted_pages: list[dict[str, Any]]) -> list[int]:
        ids: list[int] = []
        for p in deleted_pages:
            storage.delete_file_quietly(p.get("raw_path", ""))
            storage.delete_file_quietly(p.get("clean_path", ""))
            storage.delete_file_quietly(p.get("thumb_path", ""))
            ids.append(int(p["id"]))
        if ids:
            coordinator.events.publish("page_deleted", {"book_id": book_id, "page_ids": ids})
        return ids

    @app.post("/api/books/{book_id}/pages/batch-delete")
    def batch_delete_pages(book_id: int, body: BatchDeletePagesRequest) -> dict[str, Any]:
        if db.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        deleted = db.delete_pages_batch(book_id, body.page_ids)
        ids = _cleanup_deleted_pages(book_id, deleted)
        return {"deleted_count": len(ids), "deleted_ids": ids}

    @app.post("/api/books/{book_id}/pages/delete-flagged")
    def delete_flagged_pages(book_id: int) -> dict[str, Any]:
        if db.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        deleted = db.delete_flagged_pages(book_id)
        ids = _cleanup_deleted_pages(book_id, deleted)
        return {"deleted_count": len(ids), "deleted_ids": ids}

    @app.delete("/api/books/{book_id}/pages")
    def clear_book_pages(book_id: int) -> dict[str, Any]:
        if db.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        deleted = db.clear_book_pages(book_id)
        ids = _cleanup_deleted_pages(book_id, deleted)
        return {"deleted_count": len(ids)}

    @app.get("/api/pages/{page_id}")
    def get_page(page_id: int) -> dict[str, Any]:
        page = db.get_page(page_id)
        if page is None:
            raise HTTPException(status_code=404, detail="页面不存在")
        return page

    @app.patch("/api/pages/{page_id}")
    def update_page(page_id: int, body: PageUpdate) -> dict[str, Any]:
        existing = db.get_page(page_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="页面不存在")
        updates: dict[str, Any] = {}
        if body.clean_text is not None:
            updates["clean_text"] = body.clean_text
        if body.status is not None:
            updates["status"] = body.status
        if body.seq_no is not None:
            updates["seq_no"] = body.seq_no
        updated = db.update_page(page_id, **updates)
        assert updated is not None
        coordinator.events.publish(
            "page_updated",
            {"book_id": updated["book_id"], "page_id": page_id, "status": updated["status"]},
        )
        return updated

    @app.delete("/api/pages/{page_id}")
    def delete_page(page_id: int) -> dict[str, Any]:
        deleted = db.delete_page(page_id)
        if deleted is None:
            raise HTTPException(status_code=404, detail="页面不存在")
        storage.delete_file_quietly(deleted.get("raw_path", ""))
        storage.delete_file_quietly(deleted.get("clean_path", ""))
        storage.delete_file_quietly(deleted.get("thumb_path", ""))
        coordinator.events.publish(
            "page_deleted", {"book_id": deleted["book_id"], "page_id": page_id}
        )
        return {"deleted": True, "page_id": page_id}

    @app.post("/api/pages/{page_id}/reclean")
    def reclean_page(page_id: int, body: RecleanRequest) -> dict[str, Any]:
        try:
            return coordinator.reclean_page(
                page_id, profile_patch=body.profile_patch, rerun_ocr=body.rerun_ocr
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/pages/{page_id}/rotate")
    def rotate_page(page_id: int, body: RotatePageRequest) -> dict[str, Any]:
        try:
            return coordinator.rotate_page(
                page_id, delta_deg=body.delta_deg, rerun_ocr=body.rerun_ocr
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/pages/{page_id}/llm-clean")
    def llm_clean_page(page_id: int) -> dict[str, Any]:
        try:
            return coordinator.run_single_llm_clean(page_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"LLM 精校失败: {exc}") from exc

    @app.get("/api/pages/{page_id}/image/{kind}")
    def get_page_image(page_id: int, kind: str) -> FileResponse:
        page = db.get_page(page_id)
        if page is None:
            raise HTTPException(status_code=404, detail="页面不存在")
        if kind == "raw":
            rel = page.get("raw_path")
        elif kind == "clean":
            rel = page.get("clean_path") or page.get("raw_path")
        elif kind == "thumb":
            rel = page.get("thumb_path") or page.get("clean_path") or page.get("raw_path")
        else:
            raise HTTPException(status_code=400, detail="Unsupported image kind")
        if not rel:
            raise HTTPException(status_code=404, detail="图片尚未就绪")
        path = storage.resolve_path(rel)
        if not path.exists():
            raise HTTPException(status_code=404, detail="磁盘图片文件缺失")
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------
    # Pipeline Controls
    # ------------------------------------------------------------------
    @app.post("/api/pipeline/pause")
    def pause_pipeline() -> dict[str, Any]:
        coordinator.pause()
        return {"paused": True}

    @app.post("/api/pipeline/resume")
    def resume_pipeline() -> dict[str, Any]:
        coordinator.resume()
        return {"paused": False}

    @app.post("/api/pipeline/retry-failed")
    def retry_failed(book_id: int | None = None) -> dict[str, Any]:
        count = db.retry_failed_pages(book_id=book_id)
        coordinator.wake_all()
        return {"retried": count}

    @app.post("/api/books/{book_id}/reformat-markdown")
    def reformat_book_markdown(book_id: int, force: bool = Query(False)) -> dict[str, Any]:
        if db.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        pages = db.list_export_pages(book_id)
        updated_count = 0
        for p in pages:
            formatted = reformat_page_from_blocks(p)
            if not formatted:
                continue
            manually_edited = bool(p.get("clean_text")) and p.get("clean_text") != p.get("ocr_text")
            patch: dict[str, Any] = {"ocr_text": formatted}
            if force or not manually_edited:
                patch["clean_text"] = formatted
            if (p.get("ocr_text") == formatted) and (patch.get("clean_text", p.get("clean_text")) == p.get("clean_text")):
                continue
            db.update_page(int(p["id"]), **patch)
            updated_count += 1
        if updated_count > 0:
            coordinator.events.publish("book_updated", {"book_id": book_id, "updated_pages": updated_count})
        return {"updated_pages": updated_count}

    # ------------------------------------------------------------------
    # Desktop Camera Endpoints
    # ------------------------------------------------------------------
    @app.get("/api/camera/devices")
    def camera_devices() -> dict[str, Any]:
        return {"devices": coordinator.camera.list_devices(), "status": coordinator.camera.status()}

    @app.post("/api/camera/open")
    def camera_open(body: CameraOpenRequest) -> dict[str, Any]:
        try:
            st = coordinator.camera.open(index=body.index, width=body.width, height=body.height)
            return {"status": st}
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/camera/close")
    def camera_close() -> dict[str, Any]:
        return {"status": coordinator.camera.close()}

    @app.post("/api/camera/auto-trigger")
    def camera_auto_trigger(body: AutoTriggerRequest) -> dict[str, Any]:
        st = coordinator.camera.configure_auto_trigger(
            enabled=body.enabled,
            book_id=body.book_id,
            settle_ms=body.settle_ms,
            hands_on_assist=body.hands_on_assist,
            margin_ratio=body.margin_ratio,
        )
        return {"status": st}

    @app.get("/api/camera/stream")
    def camera_stream() -> StreamingResponse:
        if not coordinator.camera.status()["is_open"]:
            raise HTTPException(status_code=409, detail="摄像头未开启")
        return StreamingResponse(
            coordinator.camera.mjpeg_generator(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    # ------------------------------------------------------------------
    # Phone Companion LAN Pairing & Android USB Bridge
    # ------------------------------------------------------------------
    def _build_mobile_url(request: Request, book_id: int | None) -> tuple[str, str, str, int]:
        lan_ip = detect_lan_ip()
        scheme = request.url.scheme or "http"
        port = request.url.port or cfg.port
        host_part = f"{lan_ip}:{port}" if port not in (80, 443) else lan_ip
        q = f"?book_id={int(book_id)}" if book_id is not None else ""
        return lan_ip, f"{scheme}://{host_part}/mobile{q}", scheme, int(port)

    @app.get("/api/mobile/info")
    def mobile_info(request: Request, book_id: int | None = None) -> dict[str, Any]:
        lan_ip, url, scheme, port = _build_mobile_url(request, book_id)
        q = f"?book_id={int(book_id)}" if book_id is not None else ""
        return {
            "lan_ip": lan_ip,
            "url": url,
            "usb_url": f"http://127.0.0.1:{port}/mobile{q}",
            "scheme": scheme,
            "is_https": scheme == "https",
            "svg_qr": make_qr_svg(url),
        }

    @app.get("/api/mobile/qr")
    def mobile_qr(request: Request, book_id: int | None = None) -> Response:
        _, url, _, _ = _build_mobile_url(request, book_id)
        return Response(content=make_qr_svg(url), media_type="image/svg+xml")

    @app.get("/api/android/adb-status")
    def android_adb_status(request: Request) -> dict[str, Any]:
        port = request.url.port or cfg.port
        return get_adb_status(port=int(port))

    @app.post("/api/android/adb-reverse")
    def android_adb_reverse(request: Request, body: AdbReverseRequest) -> dict[str, Any]:
        port = request.url.port or cfg.port
        return setup_adb_reverse(
            port=int(port),
            book_id=body.book_id,
            open_on_phone=body.open_on_phone,
        )

    # ------------------------------------------------------------------
    # Stage 2: Excel Templates & Structured Data Cleaning
    # ------------------------------------------------------------------
    @app.get("/api/excel-templates")
    def list_excel_templates() -> dict[str, Any]:
        return {"items": db.list_excel_templates()}

    @app.post("/api/excel-templates", status_code=201)
    def create_excel_template(body: ExcelTemplateCreate) -> dict[str, Any]:
        return db.create_excel_template(
            name=body.name,
            description=body.description,
            entry_sheet_name=body.entry_sheet_name,
            table_sheet_name=body.table_sheet_name,
            rules=body.rules,
            columns=body.columns,
            sheets=body.sheets,
        )

    @app.put("/api/excel-templates/{template_id}")
    def update_excel_template(template_id: int, body: ExcelTemplateUpdate) -> dict[str, Any]:
        tpl = db.update_excel_template(
            template_id,
            name=body.name,
            description=body.description,
            entry_sheet_name=body.entry_sheet_name,
            table_sheet_name=body.table_sheet_name,
            rules=body.rules,
            columns=body.columns,
            sheets=body.sheets,
        )
        if tpl is None:
            raise HTTPException(status_code=404, detail="Excel 模板不存在")
        return tpl

    @app.delete("/api/excel-templates/{template_id}")
    def delete_excel_template(template_id: int) -> dict[str, Any]:
        try:
            ok = db.delete_excel_template(template_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=404, detail="Excel 模板不存在")
        return {"deleted": True, "template_id": template_id}

    @app.post("/api/excel-templates/from-xlsx", status_code=201)
    async def create_template_from_xlsx(
        file: UploadFile = File(...),
        name: str = Form(""),
        force_pack: bool = Form(False),
    ) -> dict[str, Any]:
        raw_bytes = await file.read()
        try:
            wb_check = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
            sheet_count = len(wb_check.sheetnames)
            wb_check.close()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"解析 Excel 工作簿失败: {exc}") from exc

        stem = Path(file.filename or "template.xlsx").stem
        if sheet_count >= 2 or force_pack:
            try:
                parsed_sheets = read_workbook_sheets_from_xlsx(raw_bytes)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"解析多 Sheet Excel 模板失败: {exc}") from exc
            tpl_name = name.strip() or f"导入多表模板 - {stem}"
            return db.create_excel_template(
                name=tpl_name,
                description=f"从现有 .xlsx 导入 {len(parsed_sheets)} 张子表组成的多表模板包",
                rules=dict(CATBOOST_BATCH_RULES),
                sheets=parsed_sheets,
            )

        try:
            columns = read_headers_from_xlsx(raw_bytes)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"解析 Excel 表头失败: {exc}") from exc
        tpl_name = name.strip() or f"导入模板 - {stem}"
        return db.create_excel_template(
            name=tpl_name,
            description="从现有 .xlsx 第一行表头自动生成",
            columns=columns,
        )

    def _build_match_preview_payload(book_id: int, template_id: int | None) -> dict[str, Any]:
        book = db.get_book(book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        tpl = (
            db.get_excel_template(template_id)
            if template_id is not None
            else db.get_default_excel_template()
        )
        if tpl is None:
            raise HTTPException(status_code=404, detail="Excel 模板不存在")
        pages = db.list_export_pages(book_id)
        assignments = db.list_page_assignments(book_id, int(tpl["id"]))
        res = match_book_to_template_pack(book, pages, tpl, assignments)
        # Trim row preview sizes for snappy JSON delivery
        for s in res.get("sheets") or []:
            trimmed_rows = []
            for r in (s.get("rows") or [])[:100]:
                meta = r.get("_meta") or {}
                trimmed_rows.append(
                    {
                        **{k: v for k, v in r.items() if k != "_meta"},
                        "_page_no": meta.get("page_no", 0),
                        "_source": meta.get("source", "offline"),
                        "_warnings": meta.get("warnings", []),
                        "_ocr_confidence": meta.get("ocr_confidence", 0.0),
                    }
                )
            s["total_row_count"] = len(s.get("rows") or [])
            s["rows"] = trimmed_rows
        cb = res.get("catboost_dataset") or {}
        cb_rows = list(cb.get("rows") or [])
        res["catboost_dataset"] = {
            **cb,
            "total_row_count": len(cb_rows),
            "rows": cb_rows[:50],
        }
        return res

    @app.get("/api/books/{book_id}/match-preview")
    def get_book_match_preview(
        book_id: int,
        template_id: int | None = Query(None),
    ) -> dict[str, Any]:
        return _build_match_preview_payload(book_id, template_id)

    @app.put("/api/books/{book_id}/page-assignments")
    def update_page_assignment(book_id: int, body: PageAssignmentRequest) -> dict[str, Any]:
        if db.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        tpl = (
            db.get_excel_template(body.template_id)
            if body.template_id is not None
            else db.get_default_excel_template()
        )
        if tpl is None:
            raise HTTPException(status_code=404, detail="Excel 模板不存在")
        page = db.get_page(body.page_id)
        if page is None or int(page["book_id"]) != int(book_id):
            raise HTTPException(status_code=404, detail="页面不属于当前工作集")
        db.set_page_assignment(book_id, int(tpl["id"]), int(body.page_id), body.sheet_id)
        return _build_match_preview_payload(book_id, int(tpl["id"]))

    @app.post("/api/excel-templates/{template_id}/learn-alias")
    def learn_template_alias(template_id: int, body: LearnAliasRequest) -> dict[str, Any]:
        try:
            return db.learn_template_field_alias(
                template_id=template_id,
                sheet_id=body.sheet_id,
                field_key=body.field_key,
                source_alias=body.source_alias,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/books/{book_id}/pages/{page_id}/llm-extract-sheet")
    def llm_extract_page_to_sheet(
        book_id: int,
        page_id: int,
        body: LLMSheetExtractRequest,
    ) -> dict[str, Any]:
        if db.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        page = db.get_page(page_id)
        if page is None or int(page["book_id"]) != int(book_id):
            raise HTTPException(status_code=404, detail="页面不属于当前工作集")
        tpl = (
            db.get_excel_template(body.template_id)
            if body.template_id is not None
            else db.get_default_excel_template()
        )
        if tpl is None:
            raise HTTPException(status_code=404, detail="Excel 模板不存在")
        sheet_def = next(
            (s for s in (tpl.get("sheets") or []) if s.get("sheet_id") == body.sheet_id),
            None,
        )
        if sheet_def is None:
            raise HTTPException(status_code=404, detail="目标模板子表不存在")

        all_settings = db.get_settings()
        cfg = LLMConfig.from_dict(all_settings.get("llm"))
        ocr_cfg = all_settings.get("ocr") or {}
        if ocr_cfg.get("strict_offline", True) and not is_local_only_url(cfg.base_url):
            raise HTTPException(
                status_code=403,
                detail="当前已开启「涉密严格离线模式」，仅允许连接本机 127.0.0.1 / localhost 地址",
            )
        if not cfg.base_url or (not cfg.api_key and not is_local_only_url(cfg.base_url)):
            raise HTTPException(
                status_code=400,
                detail="请先在右上角「OCR 设置 → 文本级 LLM 精校设置」中填写 Base URL 与 API Key",
            )
        txt = str(page.get("clean_text") or page.get("ocr_text") or "").strip()
        if not txt:
            raise HTTPException(status_code=400, detail="当前页面尚未完成 OCR 识别或正文为空")
        try:
            rows, meta = extract_sheet_rows_with_llm(txt, sheet_def, cfg)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"大模型填表调用失败: {exc}") from exc

        db.set_page_assignment(
            book_id=book_id,
            template_id=int(tpl["id"]),
            page_id=page_id,
            sheet_id=body.sheet_id,
            llm_rows=rows,
        )
        return {"rows": rows, "count": len(rows), "meta": meta}

    @app.post("/api/books/{book_id}/data-clean/run")
    def run_data_clean(book_id: int, body: DataCleanRunRequest | None = None) -> dict[str, Any]:
        if db.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        tpl_id = body.template_id if body else None
        try:
            summary = coordinator.run_data_cleaning_sync(book_id=book_id, template_id=tpl_id)
            return {"summary": summary}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/books/{book_id}/structured-rows")
    def list_structured_rows(
        book_id: int,
        kind: str = Query("ENTRY"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        if db.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        items, total = db.list_structured_rows(book_id, kind=kind, limit=limit, offset=offset)
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "summary": db.get_structured_summary(book_id),
        }

    # ------------------------------------------------------------------
    # Export & Jobs
    # ------------------------------------------------------------------
    @app.post("/api/books/{book_id}/export")
    def export_book(book_id: int, body: ExportRequest) -> dict[str, Any]:
        if db.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="书目不存在")
        fmt = body.format.lower().strip()
        if fmt not in ("pdf", "markdown", "md", "epub", "jsonl", "xlsx", "excel"):
            raise HTTPException(status_code=400, detail=f"不支持的导出格式: {body.format}")
        job = coordinator.start_export_job(book_id=book_id, fmt=fmt, template_id=body.template_id)
        return {"job": job}

    @app.get("/api/jobs")
    def list_jobs(book_id: int | None = None, limit: int = Query(30, ge=1, le=100)) -> dict[str, Any]:
        return {"items": db.list_jobs(book_id=book_id, limit=limit)}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int) -> dict[str, Any]:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return job

    @app.get("/api/jobs/{job_id}/download")
    def download_job(job_id: int) -> FileResponse:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if job["status"] != "DONE" or not job["result_path"]:
            raise HTTPException(status_code=409, detail="导出文件尚未完成")
        path = storage.resolve_path(job["result_path"])
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="导出文件不存在")
        return FileResponse(path, filename=path.name)

    # ------------------------------------------------------------------
    # Offline OCR Hub, Hardware Recommendation & Resumable Downloads
    # ------------------------------------------------------------------
    @app.get("/api/ocr/models")
    def list_offline_ocr_models() -> dict[str, Any]:
        ob = str((db.get_settings().get("ocr") or {}).get("ollama_base_url") or "http://127.0.0.1:11434")
        return coordinator.offline_ocr_hub.get_catalog_status(ollama_base_url=ob)

    @app.post("/api/ocr/models/check")
    def check_offline_ocr_models() -> dict[str, Any]:
        ob = str((db.get_settings().get("ocr") or {}).get("ollama_base_url") or "http://127.0.0.1:11434")
        return coordinator.offline_ocr_hub.get_catalog_status(ollama_base_url=ob)

    @app.post("/api/ocr/models/{model_id}/download")
    def download_offline_ocr_model(
        model_id: str,
        body: ModelDownloadRequest | None = None,
    ) -> dict[str, Any]:
        req = body or ModelDownloadRequest()
        ob = str((db.get_settings().get("ocr") or {}).get("ollama_base_url") or "http://127.0.0.1:11434")
        try:
            return coordinator.offline_ocr_hub.start_download(
                model_id,
                use_cn_mirror=req.use_cn_mirror,
                force=req.force,
                ollama_base_url=ob,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/ocr/models/{model_id}/pause")
    def pause_offline_ocr_model(model_id: str) -> dict[str, Any]:
        try:
            return coordinator.offline_ocr_hub.pause_download(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/ocr/models/{model_id}/cancel")
    def cancel_offline_ocr_model(model_id: str) -> dict[str, Any]:
        try:
            return coordinator.offline_ocr_hub.cancel_download(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/ocr/models/{model_id}")
    def uninstall_offline_ocr_model(model_id: str) -> dict[str, Any]:
        ob = str((db.get_settings().get("ocr") or {}).get("ollama_base_url") or "http://127.0.0.1:11434")
        try:
            return coordinator.offline_ocr_hub.uninstall_model(model_id, ollama_base_url=ob)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # Settings, DeepSeek-OCR 2 On-Demand Add-on & Connectivity Tests
    # ------------------------------------------------------------------
    @app.get("/api/ocr/addons/deepseek")
    def get_deepseek_addon_status() -> dict[str, Any]:
        return coordinator.deepseek_addon.get_status()

    @app.post("/api/ocr/addons/deepseek/check")
    def check_deepseek_addon_hardware() -> dict[str, Any]:
        return coordinator.deepseek_addon.get_status()

    @app.post("/api/ocr/addons/deepseek/install")
    def install_deepseek_addon(body: AddonInstallRequest | None = None) -> dict[str, Any]:
        req = body or AddonInstallRequest()
        try:
            return coordinator.deepseek_addon.start_install(
                use_cn_mirror=req.use_cn_mirror,
                repo_id=req.repo_id,
                force=req.force,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/ocr/addons/deepseek/pause")
    def pause_deepseek_addon_install() -> dict[str, Any]:
        return coordinator.deepseek_addon.pause_install()

    @app.post("/api/ocr/addons/deepseek/cancel")
    def cancel_deepseek_addon_install() -> dict[str, Any]:
        return coordinator.deepseek_addon.cancel_install()

    @app.delete("/api/ocr/addons/deepseek")
    def uninstall_deepseek_addon() -> dict[str, Any]:
        return coordinator.deepseek_addon.uninstall()

    @app.post("/api/ocr/test-api")
    def test_ocr_api(body: OCRTestRequest) -> dict[str, Any]:
        saved_ocr = db.get_settings().get("ocr") or {}
        strict_off = body.strict_offline if body.strict_offline is not None else bool(saved_ocr.get("strict_offline", True))
        payload = {**body.model_dump(), "strict_offline": strict_off}
        cfg_ocr = OCRSettings.from_dict(payload)
        return test_deepseek_api_connection(cfg_ocr)

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return db.get_settings()

    @app.put("/api/settings")
    def update_settings(body: SettingsUpdateRequest) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        if body.ocr is not None:
            patch["ocr"] = body.ocr
        if body.llm is not None:
            patch["llm"] = body.llm
        if body.capture_profile is not None:
            patch["capture_profile"] = body.capture_profile
        return db.update_settings(patch)

    @app.post("/api/settings/test-llm")
    def test_llm(body: LLMTestRequest) -> dict[str, Any]:
        saved_ocr = db.get_settings().get("ocr") or {}
        strict_off = body.strict_offline if body.strict_offline is not None else bool(saved_ocr.get("strict_offline", True))
        cfg_llm = LLMConfig.from_dict(body.model_dump())
        if strict_off and not is_local_only_url(cfg_llm.base_url):
            return {
                "ok": False,
                "message": "当前已开启「涉密严格离线模式」，仅允许连接本机 127.0.0.1 / localhost 地址",
                "model": cfg_llm.model,
                "latency_ms": 0,
            }
        return test_llm_connection(cfg_llm)

    return app
