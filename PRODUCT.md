# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- Primary users: Individual operators and small book-digitization teams running local scanning stations on Windows PCs (and macOS/Linux) with overhead USB document cameras, browser webcams, or Android phones mounted over physical books.
- Inferred operational need (explicitly noted): Users flip physical book pages continuously while holding book edges down by hand, then inspect cleaned page quality, proofread OCR Markdown, map Excel schemas, and batch export finished deliverables without cloud dependencies.

## Product Purpose

A local-first, offline-capable optical book digitization and data-cleaning workbench (**「超级扫描仪」**) that converts raw overhead photographs, USB high-speed scanner feeds, and PDFs/folders into cleaned, dewarp-corrected flat book pages, verified OCR Markdown, structured multi-sheet Excel workbooks, searchable CJK PDFs, EPUB 3, and RAG JSONL.

Success means:
- Operators turn pages nonstop without capture lag (millisecond L0 QA telemetry + decoupled SQLite WAL queuing).
- Every scanned page is visibly improved (before/after wipe comparison verifying yellow-cast bleaching, spine shadow removal, thumb/finger erasure, and optical dewarp).
- Zero cloud lock-in and reliable offline Windows installation and execution.

## Positioning

Unlike cloud-only consumer scanner apps or bloated enterprise DMS suites, **超级扫描仪** combines:
1. **Zero-storage Android companion capture (`/mobile`)**: Uses `ImageCapture.takePhoto()` via USB ADB or LAN `--auto-https` to stream captures from browser memory directly to the PC without saving photos to phone storage.
2. **Hands-on-page margin awareness & erasure**: Left/right 16% thumb-hold safe zones allow operators to hold thick book pages flat while automatic settle-detection waits for turning hands to clear (`0.38s`), then erases margin fingers during background L1 OpenCV processing.
3. **Decoupled 100k-page SQLite WAL pipeline + customizable Excel schema cleaning**: Separates real-time capture from L1 optical cleanup, multi-engine OCR (`RapidOCR` / `DeepSeek-OCR 2` / hybrid fallback), optional L3 LLM proofreading, and flexible Excel column mapping.

## Operating Context

- **Desktop workstation (`1440×900+`, Chrome/Edge on Windows/macOS)**: 5 primary views in a focused studio workbench — **采集 (Capture)**, **流水线 (Pipeline Queue)**, **复核 (Before/After Visual & OCR Review)**, **清洗 (Data Cleaning & Excel Schema Mapping)**, **导出 (5-Format Export)** — plus global book collection switcher and OCR/LLM settings modal.
- **Mobile companion (`/mobile`, Android Chrome portrait)**: Handheld or overhead-mounted live camera capture UI with book selector, 2-page spread split toggle, zero-storage / Wi-Fi / queue status readouts, hands-on thumb safe-zone overlay, auto-capture settle trigger, flashlight torch, and undo.
- **Physical inputs**: USB foot pedal / keyboard `Space` shutter, `Backspace` undo retake, `J`/`K` page stepping, `A` quick approve, `Ctrl+S` save OCR text.

## Capabilities and Constraints

- **Frontend Stack**: Existing vanilla static HTML, CSS, and modular ES modules (`book_cleaner/web/index.html`, `mobile.html`, `static/*.js`, `static/styles.css`) served directly by FastAPI on localhost/LAN (`127.0.0.1:8765`).
- **Integration Constraint**: All existing DOM element IDs, FastAPI endpoints, 22 pytest integration tests, JS module contracts, and Windows batch launch scripts (`start_windows.bat`, `start_windows_https.bat`) must remain intact and compatible.
- **Offline Requirement**: Must operate 100% offline with zero external CDN runtime dependencies (`RapidOCR` built-in CPU OCR; China mirror support during initial setup).
- **Scale Target**: Proven architecture for 100,000-page SQLite WAL queues and SHA-256 sharded disk storage (`storage/book_<id>/<xx>/`).

## Brand Commitments

- **UI Product Name**: **超级扫描仪** (`SuperScanner`)
- **CLI / Python Package Name**: `book-cleaner` (must remain unchanged for compatibility with existing scripts and tests).

## Evidence on Hand

- Built-in 7-page interactive demo book generator (`seedDemoBtn` -> double-page spread split, yellow paper bleaching, curvature dewarping, margin finger removal, blur/duplicate alerts) in [book_cleaner/](book_cleaner/).
- Automated pytest suite (`22 passed` via `uv run pytest -v`) and live FastAPI server running at `http://127.0.0.1:8765/`.
- Windows deployment scripts and user-verified installation workflow ([README.md](README.md), [start_windows_https.bat](start_windows_https.bat)).

## Product Principles

1. **Viewfinder and page clarity lead the workspace**: Live camera framing and before/after page inspection should occupy the clear visual majority rather than shrinking behind heavy sidebars or control chrome.
2. **Instant capture rhythm, deferred heavy work**: Keep shutter triggers, keyboard shortcuts, and L0 blur/exposure checks effortless and immediately scannable while background workers handle L1/L2 processing.
3. **Calm, quiet chrome without boxed clutter**: Controls, toggles, and telemetry should breathe cleanly without dense card-inside-card borders, harsh multi-color status badges, or cramped toolbars.
4. **Trust through visual proof**: Side-by-side / split-slider comparison of raw scan vs. cleaned output and readable OCR Markdown sit side-by-side so operators verify quality in seconds.
5. **Cross-device continuity**: Desktop studio and Android `/mobile` capture share one restrained instrument identity and unmistakable shutter ergonomics.
