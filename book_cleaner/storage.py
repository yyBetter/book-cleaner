"""Sharded disk storage and EXIF-safe image encoding/decoding helpers."""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps


def compute_sha256(data: bytes) -> str:
    """Compute SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def decode_image_with_exif(raw_bytes: bytes) -> np.ndarray:
    """Decode image bytes honoring EXIF orientation, returning OpenCV BGR uint8 array."""
    if not raw_bytes:
        raise ValueError("Empty image payload")

    with Image.open(io.BytesIO(raw_bytes)) as img:
        transposed = ImageOps.exif_transpose(img)
        if transposed.mode in ("RGBA", "LA") or (
            transposed.mode == "P" and "transparency" in transposed.info
        ):
            rgba = transposed.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            rgb = bg
        else:
            rgb = transposed.convert("RGB")
        arr = np.asarray(rgb, dtype=np.uint8)

    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def encode_jpeg(bgr: np.ndarray, quality: int = 92) -> bytes:
    """Encode BGR image as JPEG bytes."""
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("Failed to encode JPEG image")
    return buf.tobytes()


def encode_png(bgr: np.ndarray, compression: int = 3) -> bytes:
    """Encode BGR or grayscale image as PNG bytes."""
    ok, buf = cv2.imencode(".png", bgr, [int(cv2.IMWRITE_PNG_COMPRESSION), int(compression)])
    if not ok:
        raise RuntimeError("Failed to encode PNG image")
    return buf.tobytes()


def make_thumbnail(bgr: np.ndarray, max_width: int = 420) -> np.ndarray:
    """Create aspect-preserving thumbnail image."""
    h, w = bgr.shape[:2]
    if w <= max_width or w == 0:
        return bgr.copy()
    scale = max_width / float(w)
    new_h = max(1, int(round(h * scale)))
    return cv2.resize(bgr, (max_width, new_h), interpolation=cv2.INTER_AREA)


class ShardedStorage:
    """Two-hex sharded filesystem storage designed for 100k+ page assets."""

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.storage_dir = self.base_dir / "storage"
        self.exports_dir = self.base_dir / "exports"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

    def shard_for(self, sha256: str) -> str:
        clean_hex = (sha256 or "00").strip().lower()
        return clean_hex[:2] if len(clean_hex) >= 2 else "00"

    def page_dir(self, book_id: int, sha256: str) -> Path:
        d = self.storage_dir / f"book_{int(book_id)}" / self.shard_for(sha256)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def page_paths(self, book_id: int, page_id: int, sha256: str) -> dict[str, Path]:
        d = self.page_dir(book_id, sha256)
        return {
            "raw": d / f"{int(page_id)}_raw.jpg",
            "clean": d / f"{int(page_id)}_clean.png",
            "thumb": d / f"{int(page_id)}_thumb.jpg",
        }

    def export_dir(self, book_id: int) -> Path:
        d = self.exports_dir / f"book_{int(book_id)}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def to_rel(self, path: Path) -> str:
        resolved = Path(path).resolve()
        try:
            return resolved.relative_to(self.base_dir).as_posix()
        except ValueError:
            return str(resolved)

    def resolve_path(self, path_str: str) -> Path:
        p = Path(path_str)
        if p.is_absolute():
            return p
        return (self.base_dir / p).resolve()

    def save_image_bytes_atomic(self, path: Path | str, data: bytes) -> Path:
        target = self.resolve_path(str(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
            os.replace(tmp, target)
        except Exception:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise
        return target

    def delete_file_quietly(self, path_str: str) -> None:
        if not path_str:
            return
        try:
            p = self.resolve_path(path_str)
            if p.exists() and p.is_file():
                p.unlink()
        except OSError:
            pass
