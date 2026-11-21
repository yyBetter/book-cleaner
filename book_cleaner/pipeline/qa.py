"""L0 Capture QA: resolution-independent sharpness, brightness, and perceptual duplicate checks."""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass(slots=True)
class QAResult:
    blur_score: float
    brightness: float
    dhash: str
    is_blurry: bool
    is_dark: bool
    is_overexposed: bool
    is_duplicate: bool
    flags: list[str] = field(default_factory=list)
    passed: bool = True

    def to_dict(self) -> dict:
        return {
            "blur_score": round(self.blur_score, 2),
            "brightness": round(self.brightness, 2),
            "dhash": self.dhash,
            "is_blurry": self.is_blurry,
            "is_dark": self.is_dark,
            "is_overexposed": self.is_overexposed,
            "is_duplicate": self.is_duplicate,
            "flags": list(self.flags),
            "passed": self.passed,
        }


def compute_blur_score(bgr: np.ndarray, standard_width: int = 1200) -> float:
    """Compute resolution-normalized Laplacian variance sharpness score."""
    if bgr.size == 0:
        return 0.0
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    h, w = gray.shape[:2]
    if w > standard_width and w > 0:
        scale = standard_width / float(w)
        gray = cv2.resize(
            gray,
            (standard_width, max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    return float(lap.var())


def compute_brightness(bgr: np.ndarray) -> float:
    """Compute mean luminance (0-255)."""
    if bgr.size == 0:
        return 0.0
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    return float(np.mean(gray))


def compute_dhash(bgr: np.ndarray, hash_size: int = 8) -> str:
    """Compute 64-bit difference hash (dHash) as a 16-char hex string."""
    if bgr.size == 0:
        return "0" * 16
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    resized = cv2.resize(gray, (hash_size + 1, hash_size), interpolation=cv2.INTER_AREA)
    diff = resized[:, 1:] > resized[:, :-1]
    bits = 0
    for val in diff.flatten():
        bits = (bits << 1) | int(bool(val))
    hex_len = (hash_size * hash_size) // 4
    return f"{bits:0{hex_len}x}"


def hamming_distance(h1: str, h2: str) -> int:
    """Compute Hamming distance between two hex hash strings."""
    if not h1 or not h2 or len(h1) != len(h2):
        return 64
    try:
        return (int(h1, 16) ^ int(h2, 16)).bit_count()
    except ValueError:
        return 64


def evaluate_capture_qa(
    bgr: np.ndarray,
    recent_dhashes: list[str] | None = None,
    *,
    blur_threshold: float = 80.0,
    dup_threshold: int = 6,
    dark_threshold: float = 48.0,
    overexposed_threshold: float = 242.0,
) -> QAResult:
    """Run fast L0 quality checks at capture/ingestion time."""
    blur_score = compute_blur_score(bgr)
    brightness = compute_brightness(bgr)
    dhash = compute_dhash(bgr)

    is_blurry = blur_score < blur_threshold
    is_dark = brightness < dark_threshold
    is_overexposed = brightness > overexposed_threshold
    is_duplicate = False
    if recent_dhashes:
        for prev in recent_dhashes:
            if hamming_distance(dhash, prev) <= dup_threshold:
                is_duplicate = True
                break

    flags: list[str] = []
    if is_blurry:
        flags.append("blurry")
    if is_dark:
        flags.append("too_dark")
    if is_overexposed:
        flags.append("overexposed")
    if is_duplicate:
        flags.append("duplicate_page")

    return QAResult(
        blur_score=blur_score,
        brightness=brightness,
        dhash=dhash,
        is_blurry=is_blurry,
        is_dark=is_dark,
        is_overexposed=is_overexposed,
        is_duplicate=is_duplicate,
        flags=flags,
        passed=len(flags) == 0,
    )
