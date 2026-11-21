"""Configuration management for Book Cleaner."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    """Application runtime configuration loaded from environment variables or overrides."""

    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("BOOK_CLEANER_DATA_DIR", "./data")).resolve()
    )
    host: str = field(default_factory=lambda: os.environ.get("BOOK_CLEANER_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("BOOK_CLEANER_PORT", "8765")))
    clean_workers: int = field(
        default_factory=lambda: int(os.environ.get("BOOK_CLEANER_CLEAN_WORKERS", "2"))
    )
    ocr_workers: int = field(
        default_factory=lambda: int(os.environ.get("BOOK_CLEANER_OCR_WORKERS", "1"))
    )
    llm_workers: int = field(
        default_factory=lambda: int(os.environ.get("BOOK_CLEANER_LLM_WORKERS", "2"))
    )
    blur_warn_threshold: float = field(
        default_factory=lambda: float(os.environ.get("BOOK_CLEANER_BLUR_THRESHOLD", "80.0"))
    )
    dup_hamming_threshold: int = field(
        default_factory=lambda: int(os.environ.get("BOOK_CLEANER_DUP_HAMMING", "6"))
    )
    ocr_review_threshold: float = field(
        default_factory=lambda: float(os.environ.get("BOOK_CLEANER_OCR_REVIEW_THRESHOLD", "0.82"))
    )
    dark_threshold: float = 48.0
    overexposed_threshold: float = 252.0

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "book_cleaner.db"

    @property
    def storage_dir(self) -> Path:
        return self.data_dir / "storage"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
