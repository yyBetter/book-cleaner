"""Exporters: Searchable PDF (PyMuPDF), Markdown, EPUB 3, and RAG JSONL."""

from __future__ import annotations

import html
import json
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pymupdf
from PIL import Image

from book_cleaner.storage import ShardedStorage

if TYPE_CHECKING:
    from book_cleaner.pipeline.pii_redactor import ConsistentPIIRedactor


def export_markdown(
    book: dict[str, Any],
    pages: list[dict[str, Any]],
    out_path: Path,
    *,
    redactor: "ConsistentPIIRedactor | None" = None,
) -> Path:
    """Export cleaned book pages into structured Markdown with YAML frontmatter."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    use_redactor = bool(redactor and redactor.enabled)

    title_raw = str(book.get("title", "未命名书籍"))
    author_raw = str(book.get("author", ""))
    title_out = redactor.redact_text(title_raw) if use_redactor else title_raw
    author_out = redactor.redact_text(author_raw) if use_redactor else author_raw

    page_bodies: list[tuple[Any, Any, float, str]] = []
    for p in pages:
        seq = p.get("seq_no", 0)
        side = p.get("spread_side", "S")
        conf = float(p.get("ocr_confidence", 0.0))
        body = (p.get("clean_text") or p.get("ocr_text") or "").strip()
        if use_redactor and body:
            body = redactor.redact_text(body)
        page_bodies.append((seq, side, conf, body))

    lines = [
        "---",
        f'title: "{title_out.replace(chr(34), chr(39))}"',
        f'author: "{author_out.replace(chr(34), chr(39))}"',
        f'isbn: "{str(book.get("isbn", "")).replace(chr(34), chr(39))}"',
        f"page_count: {len(pages)}",
        f'exported_at: "{exported_at}"',
    ]
    if use_redactor and redactor is not None:
        r_stats = redactor.stats()
        lines.append("pii_redacted: true")
        lines.append(f"pii_redactions: {r_stats['total_redactions']}")
    lines.extend(
        [
            "---",
            "",
            f"# {title_out or '未命名书籍'}",
            "",
        ]
    )
    for seq, side, conf, body in page_bodies:
        lines.append(f"<!-- page: {seq} side: {side} conf: {conf:.2f} -->")
        lines.append(body if body else "_（本页暂无识别文本）_")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def _insert_invisible_line(
    pdf_page: pymupdf.Page,
    x: float,
    y: float,
    text: str,
    fontsize: float,
) -> None:
    if not text.strip():
        return
    try:
        pdf_page.insert_text(
            (x, y),
            text,
            fontsize=fontsize,
            fontname="china-ss",
            render_mode=3,
        )
    except Exception:
        safe_ascii = text.encode("ascii", errors="ignore").decode("ascii")
        if safe_ascii.strip():
            pdf_page.insert_text(
                (x, y),
                safe_ascii,
                fontsize=fontsize,
                fontname="helv",
                render_mode=3,
            )


def export_searchable_pdf(
    book: dict[str, Any],
    pages: list[dict[str, Any]],
    out_path: Path,
    storage: ShardedStorage,
) -> Path:
    """Export cleaned page images into a searchable PDF with invisible CJK text layer."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()

    try:
        for p in pages:
            img_rel = p.get("clean_path") or p.get("raw_path") or ""
            if not img_rel:
                continue
            img_path = storage.resolve_path(img_rel)
            if not img_path.exists():
                continue

            with Image.open(img_path) as im:
                w_px, h_px = im.size
            if w_px <= 0 or h_px <= 0:
                continue

            page_w_pt = 595.0
            scale = page_w_pt / float(w_px)
            page_h_pt = max(100.0, float(h_px) * scale)
            pdf_page = doc.new_page(width=page_w_pt, height=page_h_pt)
            pdf_page.insert_image(pymupdf.Rect(0, 0, page_w_pt, page_h_pt), filename=str(img_path))

            clean_text = (p.get("clean_text") or "").strip()
            ocr_text = (p.get("ocr_text") or "").strip()
            blocks = p.get("ocr_blocks") or []

            # If blocks match unedited OCR text, overlay at exact coordinates; otherwise embed clean_text lines.
            if blocks and (not clean_text or clean_text == ocr_text):
                for blk in blocks:
                    txt = str(blk.get("text", "")).strip()
                    bbox = blk.get("bbox") or []
                    if not txt or len(bbox) < 4:
                        continue
                    x0 = min(pt[0] for pt in bbox) * scale
                    y0 = min(pt[1] for pt in bbox) * scale
                    y1 = max(pt[1] for pt in bbox) * scale
                    box_h = max(6.0, y1 - y0)
                    fontsize = max(6.0, min(42.0, box_h * 0.82))
                    baseline_y = min(page_h_pt - 4.0, y0 + box_h * 0.85)
                    _insert_invisible_line(pdf_page, max(2.0, x0), baseline_y, txt, fontsize)
            elif clean_text:
                lines = [ln.strip() for ln in clean_text.splitlines() if ln.strip()]
                step_y = min(22.0, (page_h_pt - 40.0) / float(max(1, len(lines))))
                y_cursor = 28.0
                for ln in lines:
                    _insert_invisible_line(pdf_page, 24.0, y_cursor, ln, 10.5)
                    y_cursor += max(10.0, step_y)
                    if y_cursor > page_h_pt - 12.0:
                        break

        if len(doc) == 0:
            blank = doc.new_page(width=595.0, height=842.0)
            _insert_invisible_line(blank, 48.0, 72.0, str(book.get("title", "Empty Book")), 12.0)

        doc.set_metadata(
            {
                "title": str(book.get("title", "")),
                "author": str(book.get("author", "")),
                "creator": "Book Cleaner",
            }
        )
        doc.save(str(out_path), garbage=3, deflate=True)
    finally:
        doc.close()

    return out_path


def _parse_pipe_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [c.replace(r"\|", "|").strip() for c in re.split(r"(?<!\\)\|", stripped)]


def _is_pipe_table_block(blk: str) -> bool:
    lines = [ln.strip() for ln in blk.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    if "|" not in lines[0] or "|" not in lines[1]:
        return False
    sep_cells = _parse_pipe_row(lines[1])
    return bool(sep_cells) and all(re.fullmatch(r"[:\-\s]+", c or "-") for c in sep_cells)


def _render_pipe_table_xhtml(blk: str) -> str:
    lines = [ln.strip() for ln in blk.splitlines() if ln.strip()]
    headers = _parse_pipe_row(lines[0])
    th_html = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body_rows = []
    for ln in lines[2:]:
        cells = _parse_pipe_row(ln)
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        tds = "".join(f"<td>{html.escape(c)}</td>" for c in cells[: len(headers)])
        body_rows.append(f"<tr>{tds}</tr>")
    tbody_html = "".join(body_rows)
    return f"<table><thead><tr>{th_html}</tr></thead><tbody>{tbody_html}</tbody></table>"


def export_epub(book: dict[str, Any], pages: list[dict[str, Any]], out_path: Path) -> Path:
    """Export cleaned book text into a standards-compliant EPUB 3 archive."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    book_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"book-cleaner-{book.get('id', 0)}"))
    title = html.escape(str(book.get("title") or "未命名书籍"))
    author = html.escape(str(book.get("author") or "Book Cleaner"))
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    chunk_size = 20
    chunks: list[list[dict[str, Any]]] = [
        pages[i : i + chunk_size] for i in range(0, len(pages), chunk_size)
    ] or [[]]

    container_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "  <rootfiles>\n"
        '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n"
    )

    style_css = (
        "body { font-family: serif; line-height: 1.75; margin: 1.4em; }\n"
        "h1, h2 { font-family: sans-serif; margin-top: 1.4em; }\n"
        ".page-break { color: #888; font-size: 0.8em; border-top: 1px solid #ddd; "
        "margin-top: 1.8em; padding-top: 0.4em; }\n"
        "p { margin: 0.65em 0; text-align: justify; }\n"
        "table { width: 100%; border-collapse: collapse; margin: 1em 0; font-size: 0.92em; }\n"
        "th, td { border: 1px solid #ccc; padding: 6px 8px; text-align: left; }\n"
        "th { background: #f4f4f2; font-weight: bold; }\n"
    )

    chapter_files: list[tuple[str, str, str]] = []  # (id, href, title)
    chapter_contents: dict[str, str] = {}

    for idx, group in enumerate(chunks, start=1):
        chap_id = f"chap{idx}"
        href = f"{chap_id}.xhtml"
        if group:
            start_seq = group[0].get("seq_no", idx)
            end_seq = group[-1].get("seq_no", idx)
            chap_title = f"第 {start_seq} - {end_seq} 页" if start_seq != end_seq else f"第 {start_seq} 页"
        else:
            chap_title = "正文"

        body_parts: list[str] = [f"<h1>{html.escape(chap_title)}</h1>"]
        for p in group:
            seq = p.get("seq_no", 0)
            body_parts.append(f'<div class="page-break" id="page-{seq}">第 {seq} 页</div>')
            text = (p.get("clean_text") or p.get("ocr_text") or "").strip()
            if not text:
                body_parts.append("<p><em>（本页无文本）</em></p>")
                continue
            for block in text.split("\n\n"):
                blk = block.strip()
                if not blk:
                    continue
                if blk.startswith("## "):
                    body_parts.append(f"<h2>{html.escape(blk[3:].strip())}</h2>")
                elif blk.startswith("# "):
                    body_parts.append(f"<h1>{html.escape(blk[2:].strip())}</h1>")
                elif _is_pipe_table_block(blk):
                    body_parts.append(_render_pipe_table_xhtml(blk))
                else:
                    body_parts.append(f"<p>{html.escape(blk).replace(chr(10), '<br/>')}</p>")

        xhtml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<!DOCTYPE html>\n"
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">\n'
            "<head>\n"
            f"  <title>{html.escape(chap_title)}</title>\n"
            '  <link rel="stylesheet" type="text/css" href="style.css"/>\n'
            "</head>\n"
            "<body>\n"
            + "\n".join(body_parts)
            + "\n</body>\n</html>\n"
        )
        chapter_files.append((chap_id, href, chap_title))
        chapter_contents[href] = xhtml

    nav_items = "\n".join(
        f'      <li><a href="{href}">{html.escape(ctitle)}</a></li>'
        for _, href, ctitle in chapter_files
    )
    nav_xhtml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
        f"<head><title>{title}</title></head>\n"
        "<body>\n"
        '  <nav epub:type="toc" id="toc">\n'
        f"    <h1>{title}</h1>\n"
        "    <ol>\n"
        f"{nav_items}\n"
        "    </ol>\n"
        "  </nav>\n"
        "</body>\n</html>\n"
    )

    ncx_points = "\n".join(
        f'    <navPoint id="navPoint-{i}" playOrder="{i}">'
        f"<navLabel><text>{html.escape(ctitle)}</text></navLabel>"
        f'<content src="{href}"/></navPoint>'
        for i, (_, href, ctitle) in enumerate(chapter_files, start=1)
    )
    toc_ncx = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        "  <head>\n"
        f'    <meta name="dtb:uid" content="urn:uuid:{book_uuid}"/>\n'
        '    <meta name="dtb:depth" content="1"/>\n'
        "  </head>\n"
        f"  <docTitle><text>{title}</text></docTitle>\n"
        "  <navMap>\n"
        f"{ncx_points}\n"
        "  </navMap>\n"
        "</ncx>\n"
    )

    manifest_items = "\n".join(
        [
            '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '    <item id="css" href="style.css" media-type="text/css"/>',
        ]
        + [
            f'    <item id="{cid}" href="{href}" media-type="application/xhtml+xml"/>'
            for cid, href, _ in chapter_files
        ]
    )
    spine_items = "\n".join(f'    <itemref idref="{cid}"/>' for cid, _, _ in chapter_files)
    content_opf = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="BookId">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="BookId">urn:uuid:{book_uuid}</dc:identifier>\n'
        f"    <dc:title>{title}</dc:title>\n"
        f"    <dc:creator>{author}</dc:creator>\n"
        "    <dc:language>zh-CN</dc:language>\n"
        f'    <meta property="dcterms:modified">{modified}</meta>\n'
        "  </metadata>\n"
        "  <manifest>\n"
        f"{manifest_items}\n"
        "  </manifest>\n"
        '  <spine toc="ncx">\n'
        f"{spine_items}\n"
        "  </spine>\n"
        "</package>\n"
    )

    with zipfile.ZipFile(out_path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container_xml, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/style.css", style_css, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/toc.ncx", toc_ncx, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", content_opf, compress_type=zipfile.ZIP_DEFLATED)
        for href, xhtml in chapter_contents.items():
            zf.writestr(f"OEBPS/{href}", xhtml, compress_type=zipfile.ZIP_DEFLATED)

    return out_path


def export_jsonl(
    book: dict[str, Any],
    pages: list[dict[str, Any]],
    out_path: Path,
    *,
    redactor: "ConsistentPIIRedactor | None" = None,
) -> Path:
    """Export one JSON object per page for downstream search indexing / RAG."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    use_redactor = bool(redactor and redactor.enabled)
    title_out = (
        redactor.redact_text(str(book.get("title", "")))
        if use_redactor and redactor is not None
        else str(book.get("title", ""))
    )
    with open(out_path, "w", encoding="utf-8") as f:
        for p in pages:
            text_val = (p.get("clean_text") or p.get("ocr_text") or "").strip()
            if use_redactor and redactor is not None and text_val:
                text_val = redactor.redact_text(text_val)
            rec = {
                "book_id": book.get("id"),
                "book_title": title_out,
                "page_id": p.get("id"),
                "seq_no": p.get("seq_no"),
                "spread_side": p.get("spread_side", "S"),
                "text": text_val,
                "ocr_confidence": round(float(p.get("ocr_confidence", 0.0)), 4),
                "qa_flags": p.get("qa_flags") or [],
                "status": p.get("status", ""),
                "pii_redacted": use_redactor,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return out_path
