"""Command-line interface for SuperScanner (book-cleaner) server, desktop tray, bulk import, export, and demo seeding."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from book_cleaner.api import create_app, detect_lan_ip
from book_cleaner.config import AppConfig
from book_cleaner.db import Database
from book_cleaner.demo_generator import generate_demo_pages
from book_cleaner.desktop_launcher import run_desktop_launcher
from book_cleaner.storage import ShardedStorage
from book_cleaner.tls import ensure_lan_self_signed_cert
from book_cleaner.worker import PipelineCoordinator


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="book-cleaner",
        description="SuperScanner (book-cleaner): 100k-page offline document capture, cleaning, RapidOCR, and export workbench.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Start local FastAPI workbench & background pipeline workers")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--data-dir", type=Path, default=Path("./data"))
    p_serve.add_argument("--ssl-certfile", type=str, default=None)
    p_serve.add_argument("--ssl-keyfile", type=str, default=None)
    p_serve.add_argument(
        "--auto-https",
        action="store_true",
        help="Auto-generate self-signed LAN HTTPS cert so Android phones unlock Wi-Fi live auto-capture",
    )

    p_desk = sub.add_parser("desktop", help="Launch 1-click SuperScanner desktop tray mode & open browser")
    p_desk.add_argument("--host", default="0.0.0.0")
    p_desk.add_argument("--port", type=int, default=8765)
    p_desk.add_argument("--data-dir", type=Path, default=None)
    p_desk.add_argument("--no-browser", action="store_true")
    p_desk.add_argument("--no-tray", action="store_true")

    p_imp = sub.add_parser("import-folder", help="Bulk import local image/PDF scans into a book")
    p_imp.add_argument("--book-title", required=True)
    p_imp.add_argument("--folder", type=Path, required=True)
    p_imp.add_argument("--data-dir", type=Path, default=Path("./data"))
    p_imp.add_argument("--split-spread", action="store_true")
    p_imp.add_argument("--no-recursive", action="store_true")

    p_exp = sub.add_parser("export", help="Export a processed book to xlsx/pdf/markdown/epub/jsonl")
    p_exp.add_argument("--book-id", type=int, required=True)
    p_exp.add_argument("--format", choices=["xlsx", "pdf", "markdown", "epub", "jsonl"], default="pdf")
    p_exp.add_argument("--out", type=Path, default=None)
    p_exp.add_argument("--data-dir", type=Path, default=Path("./data"))

    p_demo = sub.add_parser("seed-demo", help="Create a demo book and process synthetic scans locally")
    p_demo.add_argument("--title", default="演示样书：纸质书籍清洗工艺")
    p_demo.add_argument("--data-dir", type=Path, default=Path("./data"))

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        cfg = AppConfig(data_dir=args.data_dir, host=args.host, port=args.port)
        cfg.ensure_dirs()
        lan_ip = detect_lan_ip()
        certfile = args.ssl_certfile
        keyfile = args.ssl_keyfile
        if args.auto_https and not (certfile and keyfile):
            cert_p, key_p = ensure_lan_self_signed_cert(cfg.data_dir / "certs", lan_ip)
            certfile = str(cert_p)
            keyfile = str(key_p)

        app = create_app(cfg, start_workers=True)
        scheme = "https" if certfile else "http"
        print(f"超级扫描仪 · SuperScanner -> {scheme}://127.0.0.1:{args.port}/")
        print(f"Phone Companion (WiFi)    -> {scheme}://{lan_ip}:{args.port}/mobile")
        print(f"Android USB (ADB)         -> http://127.0.0.1:{args.port}/mobile")
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            ssl_certfile=certfile,
            ssl_keyfile=keyfile,
            log_level="info",
        )
        return

    if args.command == "desktop":
        raise SystemExit(
            run_desktop_launcher(
                host=args.host,
                port=args.port,
                data_dir=args.data_dir,
                open_browser=not args.no_browser,
                use_tray=not args.no_tray,
            )
        )

    cfg = AppConfig(data_dir=args.data_dir)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    storage = ShardedStorage(cfg.data_dir)
    coord = PipelineCoordinator(cfg, db, storage)

    try:
        if args.command == "import-folder":
            book = db.create_book(title=args.book_title, profile={"split_spread": bool(args.split_spread)})
            summary = coord.import_folder_sync(
                book_id=book["id"],
                folder_path=args.folder,
                recursive=not args.no_recursive,
            )
            print(f"Imported into book #{book['id']} ({book['title']}): {summary}")
        elif args.command == "export":
            out = coord.run_export_sync(book_id=args.book_id, fmt=args.format, out_path=args.out)
            print(f"Exported book #{args.book_id} -> {out}")
        elif args.command == "seed-demo":
            book = db.create_book(title=args.title)
            created = 0
            for fname, raw_bytes in generate_demo_pages():
                override = {"split_spread": True} if "double_spread" in fname else None
                res = coord.ingest_image_bytes(
                    book_id=book["id"],
                    raw_bytes=raw_bytes,
                    source_type="demo_seed",
                    source_ref=fname,
                    profile_override=override,
                )
                created += len(res["pages"])
            print(f"Seeded demo book #{book['id']} with {created} pages.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
