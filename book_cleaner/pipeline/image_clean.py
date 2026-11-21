"""L1 Local OpenCV Image Cleaning: spread split, crop, deskew, dewarp, finger removal, shadow flattening."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import cv2
import numpy as np


EnhanceMode = Literal["clean_color", "grayscale_crisp", "bw_sauvola"]


@dataclass(slots=True)
class CleanProfile:
    split_spread: bool = False
    auto_crop: bool = True
    dewarp: bool = True
    remove_fingers: bool = True
    enhance_mode: EnhanceMode = "clean_color"
    denoise: bool = True
    deskew: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CleanProfile":
        if not data:
            return cls()
        mode = str(data.get("enhance_mode", "clean_color"))
        if mode not in ("clean_color", "grayscale_crisp", "bw_sauvola"):
            mode = "clean_color"
        return cls(
            split_spread=bool(data.get("split_spread", False)),
            auto_crop=bool(data.get("auto_crop", True)),
            dewarp=bool(data.get("dewarp", True)),
            remove_fingers=bool(data.get("remove_fingers", True)),
            enhance_mode=mode,  # type: ignore[arg-type]
            denoise=bool(data.get("denoise", True)),
            deskew=bool(data.get("deskew", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CleanResult:
    image: np.ndarray
    diagnostics: dict[str, Any] = field(default_factory=dict)


def split_double_spread(bgr: np.ndarray, split_spread: bool = False) -> list[tuple[str, np.ndarray]]:
    """Split a landscape 2-page spread at the central spine gutter into Left and Right pages."""
    h, w = bgr.shape[:2]
    if not split_spread or w <= int(h * 1.15) or w < 200:
        return [("S", bgr)]

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    y_start = int(h * 0.12)
    y_end = max(y_start + 10, int(h * 0.88))
    band = gray[y_start:y_end, :]

    x_lo = int(w * 0.36)
    x_hi = int(w * 0.64)
    roi = band[:, x_lo:x_hi].astype(np.float32)
    if roi.shape[1] < 20:
        return [("S", bgr)]

    # Vertical gradient (horizontal strokes) is strong in text columns and minimal in the spine gutter.
    sobel_y = np.abs(cv2.Sobel(roi, cv2.CV_32F, 0, 1, ksize=3))
    col_stroke_energy = np.mean(sobel_y, axis=0)

    smooth_k = max(5, int(w * 0.015) | 1)
    kernel = np.ones(smooth_k, dtype=np.float32) / float(smooth_k)
    smoothed_energy = np.convolve(col_stroke_energy, kernel, mode="same")

    rel_x = np.linspace(-1.0, 1.0, roi.shape[1], dtype=np.float32)
    center_penalty = 1.0 + 0.35 * (rel_x * rel_x)
    score = (smoothed_energy + 1.0) * center_penalty

    best_idx = int(np.argmin(score))
    split_x = x_lo + best_idx
    margin = max(2, int(w * 0.004))

    left = bgr[:, : max(20, split_x - margin)]
    right = bgr[:, min(w - 20, split_x + margin) :]
    if left.shape[1] < 40 or right.shape[1] < 40:
        return [("S", bgr)]
    return [("L", left), ("R", right)]


def _order_quad_points(pts: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]  # top-left
    rect[2] = pts[np.argmax(s)]  # bottom-right
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]  # top-right
    rect[3] = pts[np.argmax(diff)]  # bottom-left
    return rect


def detect_and_crop_page(bgr: np.ndarray) -> tuple[np.ndarray, bool]:
    """Detect outer 4-corner page boundary on desk background and warp to upright rectangle."""
    h, w = bgr.shape[:2]
    if h < 100 or w < 100:
        return bgr, False

    max_dim = 1000.0
    scale = min(1.0, max_dim / float(max(h, w)))
    small = (
        cv2.resize(bgr, (int(round(w * scale)), int(round(h * scale))), interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else bgr
    )
    sh, sw = small.shape[:2]
    img_area = float(sh * sw)

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    # Check if outer perimeter has a non-paper border (desk background)
    border_pixels = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
    inner_median = float(np.median(gray[sh // 6 : (5 * sh) // 6, sw // 6 : (5 * sw) // 6]))
    border_median = float(np.median(border_pixels))
    has_dark_border = (inner_median - border_median) > 18.0

    candidates: list[np.ndarray] = []

    edges = cv2.Canny(blurred, 40, 140)
    edges = cv2.dilate(edges, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)), iterations=1)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7)))
    cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates.extend(cnts)

    if has_dark_border:
        _, otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        otsu = cv2.morphologyEx(otsu, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)))
        cnts2, _ = cv2.findContours(otsu, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates.extend(cnts2)

    best_quad: np.ndarray | None = None
    best_area = 0.0
    for cnt in sorted(candidates, key=cv2.contourArea, reverse=True)[:10]:
        area = float(cv2.contourArea(cnt))
        if area < 0.35 * img_area or area > 0.985 * img_area:
            continue
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.025 * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            # Avoid clipping tight full-frame scans unless border actually looks like desk or quad >= 52%
            if area > best_area and (has_dark_border or area >= 0.52 * img_area):
                best_quad = approx.reshape(4, 2).astype(np.float32)
                best_area = area
                break

    if best_quad is None:
        return bgr, False

    pts_orig = _order_quad_points(best_quad / scale)
    tl, tr, br, bl = pts_orig

    width_a = float(np.linalg.norm(br - bl))
    width_b = float(np.linalg.norm(tr - tl))
    max_w = max(100, int(round(max(width_a, width_b))))

    height_a = float(np.linalg.norm(tr - br))
    height_b = float(np.linalg.norm(tl - bl))
    max_h = max(100, int(round(max(height_a, height_b))))

    # Skip degenerate aspect warps
    if max_w < w * 0.4 or max_h < h * 0.4:
        return bgr, False

    dst = np.array(
        [[0, 0], [max_w - 1, 0], [max_w - 1, max_h - 1], [0, max_h - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(pts_orig, dst)
    warped = cv2.warpPerspective(
        bgr,
        matrix,
        (max_w, max_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return warped, True


def deskew_small_angle(bgr: np.ndarray) -> tuple[np.ndarray, float]:
    """Estimate small residual text-line tilt (±12 deg) and rotate upright."""
    h, w = bgr.shape[:2]
    if h < 120 or w < 120:
        return bgr, 0.0

    scale = min(1.0, 1000.0 / float(max(h, w)))
    small = (
        cv2.resize(bgr, (int(round(w * scale)), int(round(h * scale))), interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else bgr
    )
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    # Use blackhat to isolate dark text strokes regardless of uneven lighting
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (19, 5))
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)
    _, mask = cv2.threshold(blackhat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Connect characters along lines horizontally
    line_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 2))
    connected = cv2.dilate(mask, line_kernel, iterations=1)

    edges = cv2.Canny(connected, 50, 150)
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180.0,
        threshold=60,
        minLineLength=max(40, int(small.shape[1] * 0.12)),
        maxLineGap=15,
    )

    angles: list[float] = []
    if lines is not None and len(lines) > 0:
        for line in np.asarray(lines).reshape(-1, 4):
            x1, y1, x2, y2 = line
            dx = float(x2 - x1)
            dy = float(y2 - y1)
            if abs(dx) < 1e-3:
                continue
            deg = float(np.degrees(np.arctan2(dy, dx)))
            if -12.0 <= deg <= 12.0:
                angles.append(deg)

    if len(angles) < 3:
        return bgr, 0.0

    median_angle = float(np.median(angles))
    if abs(median_angle) < 0.35:
        return bgr, 0.0

    center = (w * 0.5, h * 0.5)
    rot_m = cv2.getRotationMatrix2D(center, median_angle, 1.0)
    rotated = cv2.warpAffine(
        bgr,
        rot_m,
        (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return rotated, round(median_angle, 2)


def dewarp_curved_page(bgr: np.ndarray) -> tuple[np.ndarray, float]:
    """Flatten gentle spine/book-page text line curvature using local baseline tracking."""
    h, w = bgr.shape[:2]
    if h < 180 or w < 180:
        return bgr, 0.0

    scale = min(1.0, 1000.0 / float(max(h, w)))
    small = (
        cv2.resize(bgr, (int(round(w * scale)), int(round(h * scale))), interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else bgr
    )
    sh, sw = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    # Extract dark text strokes cleanly under uneven shadows
    bg_est = cv2.GaussianBlur(gray, (31, 31), 0)
    ink = np.clip(bg_est.astype(np.int16) - gray.astype(np.int16), 0, 255).astype(np.uint8)
    ink_bin = (ink > 18).astype(np.float32)

    # Detect center text-line rows
    cx0, cx1 = int(sw * 0.38), int(sw * 0.62)
    center_profile = np.mean(ink_bin[:, cx0:cx1], axis=1)
    if float(np.max(center_profile)) < 0.03:
        return bgr, 0.0

    # Find local row peaks inside central 80% vertical band
    y_margin = int(sh * 0.10)
    line_ys: list[int] = []
    min_line_gap = max(8, int(sh * 0.02))
    thresh = max(0.035, float(np.max(center_profile)) * 0.30)
    y = y_margin
    while y < sh - y_margin:
        val = center_profile[y]
        if val >= thresh and val >= center_profile[y - 1] and val >= center_profile[y + 1]:
            line_ys.append(y)
            y += min_line_gap
        else:
            y += 1

    if len(line_ys) < 3:
        return bgr, 0.0

    n_strips = 20
    xs_norm = np.linspace(0.06, 0.94, n_strips)
    strip_half_w = max(8, int(sw * 0.03))
    win_half_h = max(8, int(sh * 0.032))

    strip_offsets: list[list[float]] = [[] for _ in range(n_strips)]
    for y0 in line_ys:
        y_lo = max(0, y0 - win_half_h)
        y_hi = min(sh, y0 + win_half_h + 1)
        ys_grid = np.arange(y_lo, y_hi, dtype=np.float32)
        for idx, xn in enumerate(xs_norm):
            cx = int(xn * sw)
            patch = ink_bin[y_lo:y_hi, max(0, cx - strip_half_w) : min(sw, cx + strip_half_w)]
            row_mass = np.sum(patch, axis=1)
            total_mass = float(np.sum(row_mass))
            if total_mass > 6.0:
                centroid_y = float(np.sum(ys_grid * row_mass) / total_mass)
                strip_offsets[idx].append(centroid_y - float(y0))

    valid_x: list[float] = []
    valid_dy: list[float] = []
    for idx, vals in enumerate(strip_offsets):
        if len(vals) >= max(2, len(line_ys) // 4):
            valid_x.append(float(xs_norm[idx]))
            valid_dy.append(float(np.median(vals)))

    if len(valid_x) < 8:
        return bgr, 0.0

    # Fit smooth polynomial dy(x) and center it so middle has ~0 vertical shift
    coeffs = np.polyfit(np.array(valid_x), np.array(valid_dy), deg=3)
    x_full_norm = np.linspace(0.0, 1.0, w, dtype=np.float32)
    dy_small = np.polyval(coeffs, x_full_norm).astype(np.float32)
    dy_small -= float(np.polyval(coeffs, 0.5))

    dy_full = dy_small / float(scale)
    max_allowed = 0.045 * float(h)
    dy_full = np.clip(dy_full, -max_allowed, max_allowed)

    max_shift = float(np.max(np.abs(dy_full)))
    if max_shift < 2.5:
        return bgr, 0.0

    grid_x = np.tile(np.arange(w, dtype=np.float32), (h, 1))
    grid_y = np.arange(h, dtype=np.float32)[:, None] + dy_full[None, :]
    dewarped = cv2.remap(
        bgr,
        grid_x,
        grid_y,
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return dewarped, round(max_shift, 2)


def remove_edge_fingers(bgr: np.ndarray) -> tuple[np.ndarray, int]:
    """Detect and cleanly inpaint fingers/thumbs holding left/right/bottom page edges."""
    h, w = bgr.shape[:2]
    if h < 80 or w < 80:
        return bgr, 0

    out = bgr.copy()
    ycrcb = cv2.cvtColor(out, cv2.COLOR_BGR2YCrCb)
    hsv = cv2.cvtColor(out, cv2.COLOR_BGR2HSV)

    y_ch, cr_ch, cb_ch = cv2.split(ycrcb)
    h_ch, s_ch, v_ch = cv2.split(hsv)

    skin_ycrcb = (cr_ch >= 133) & (cr_ch <= 178) & (cb_ch >= 75) & (cb_ch <= 128) & (y_ch >= 55)
    skin_hsv = (s_ch >= 24) & (s_ch <= 200) & (v_ch >= 60) & ((h_ch <= 28) | (h_ch >= 162))
    skin_mask = (skin_ycrcb & skin_hsv).astype(np.uint8) * 255

    # Restrict strictly to outer page holding margins (left 15%, right 15%, bottom 12%)
    edge_roi_mask = np.zeros((h, w), dtype=np.uint8)
    lw = max(12, int(w * 0.15))
    rw = max(12, int(w * 0.85))
    bh = min(h - 1, int(h * 0.88))
    edge_roi_mask[:, :lw] = 255
    edge_roi_mask[:, rw:] = 255
    edge_roi_mask[bh:, :] = 255

    candidate = cv2.bitwise_and(skin_mask, edge_roi_mask)
    open_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
    candidate = cv2.morphologyEx(candidate, cv2.MORPH_OPEN, open_k)
    candidate = cv2.morphologyEx(candidate, cv2.MORPH_CLOSE, close_k)

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(candidate)
    if num_labels <= 1:
        return out, 0

    img_area = float(h * w)
    min_area = max(250.0, 0.0008 * img_area)
    max_area = 0.09 * img_area

    final_mask = np.zeros((h, w), dtype=np.uint8)
    removed_count = 0
    for lbl in range(1, num_labels):
        x, y, bw, bh_box, area = stats[lbl]
        if area < min_area or area > max_area:
            continue
        touches_edge = (x <= 4) or ((x + bw) >= w - 5) or ((y + bh_box) >= h - 5)
        if not touches_edge:
            continue
        final_mask[labels == lbl] = 255
        removed_count += 1

    if removed_count == 0:
        return out, 0

    dilated = cv2.dilate(final_mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))

    # Estimate nearby paper color and inpaint locally within bounding box for speed
    ys, xs = np.where(dilated > 0)
    y0, y1 = max(0, int(ys.min()) - 20), min(h, int(ys.max()) + 21)
    x0, x1 = max(0, int(xs.min()) - 20), min(w, int(xs.max()) + 21)

    roi_bgr = out[y0:y1, x0:x1]
    roi_mask = dilated[y0:y1, x0:x1]
    ring = cv2.dilate(roi_mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17))) - roi_mask
    ring_pixels = roi_bgr[ring > 0]
    if ring_pixels.size > 0:
        # Use 80th percentile bright paper pixels in surrounding ring to avoid darkening into text
        paper_fill = np.percentile(ring_pixels, 80, axis=0).astype(np.uint8)
        roi_bgr[roi_mask > 0] = paper_fill
    inpainted = cv2.inpaint(roi_bgr, roi_mask, inpaintRadius=5, flags=cv2.INPAINT_TELEA)
    out[y0:y1, x0:x1] = inpainted
    return out, removed_count


def _measure_bg_uniformity(bgr: np.ndarray) -> float:
    """Standard deviation of 80th-percentile luminance across a 4x4 grid (lower = cleaner)."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    h, w = gray.shape[:2]
    vals: list[float] = []
    for r in range(4):
        for c in range(4):
            patch = gray[(r * h) // 4 : ((r + 1) * h) // 4, (c * w) // 4 : ((c + 1) * w) // 4]
            if patch.size > 0:
                vals.append(float(np.percentile(patch, 80)))
    return float(np.std(vals)) if vals else 0.0


def _sauvola_threshold(gray: np.ndarray, window: int = 31, k: float = 0.20, r_val: float = 128.0) -> np.ndarray:
    gray_f = gray.astype(np.float32)
    mean = cv2.boxFilter(gray_f, cv2.CV_32F, (window, window), borderType=cv2.BORDER_REPLICATE)
    sqmean = cv2.boxFilter(
        gray_f * gray_f, cv2.CV_32F, (window, window), borderType=cv2.BORDER_REPLICATE
    )
    var = np.maximum(sqmean - mean * mean, 0.0)
    std = np.sqrt(var)
    thresh = mean * (1.0 + k * (std / r_val - 1.0))
    return np.where(gray_f > thresh, 255, 0).astype(np.uint8)


def flatten_illumination_and_enhance(
    bgr: np.ndarray,
    enhance_mode: EnhanceMode = "clean_color",
    denoise: bool = True,
) -> tuple[np.ndarray, dict[str, float]]:
    """Remove spine shadows & yellow paper cast, then apply chosen output look."""
    h, w = bgr.shape[:2]
    uniformity_before = _measure_bg_uniformity(bgr)

    # Estimate slow-varying background illumination on downsampled copy for speed
    scale = min(1.0, 640.0 / float(max(h, w)))
    small = (
        cv2.resize(bgr, (max(16, int(round(w * scale))), max(16, int(round(h * scale)))), interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else bgr.copy()
    )
    ksize = max(15, int(min(small.shape[:2]) * 0.055) | 1)
    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))
    bg_small = cv2.morphologyEx(small, cv2.MORPH_CLOSE, close_k)
    blur_k = (ksize * 2 + 1) | 1
    bg_small = cv2.GaussianBlur(bg_small, (blur_k, blur_k), 0)
    bg_full = cv2.resize(bg_small, (w, h), interpolation=cv2.INTER_LINEAR).astype(np.float32)
    bg_full = np.maximum(bg_full, 48.0)

    normalized = np.clip((bgr.astype(np.float32) / bg_full) * 248.0, 0, 255).astype(np.uint8)
    if denoise:
        normalized = cv2.bilateralFilter(normalized, d=5, sigmaColor=24, sigmaSpace=24)

    if enhance_mode == "clean_color":
        # Push bright paper tones gently to pure white while keeping text/illustrations sharp
        out = normalized.astype(np.float32)
        paper_lift = (out - 18.0) * (255.0 / 222.0)
        out = np.clip(paper_lift, 0, 255).astype(np.uint8)
    elif enhance_mode == "grayscale_crisp":
        gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (0, 0), 1.2)
        sharp = cv2.addWeighted(gray, 1.32, blurred, -0.32, 0)
        sharp = np.clip((sharp.astype(np.float32) - 16.0) * (255.0 / 218.0), 0, 255).astype(np.uint8)
        sharp[sharp >= 236] = 255
        out = cv2.cvtColor(sharp, cv2.COLOR_GRAY2BGR)
    else:  # bw_sauvola
        gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
        bw = _sauvola_threshold(gray, window=31, k=0.20, r_val=128.0)
        out = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)

    uniformity_after = _measure_bg_uniformity(out)
    return out, {
        "bg_uniformity_before": round(uniformity_before, 2),
        "bg_uniformity_after": round(uniformity_after, 2),
    }


def clean_page_image(bgr: np.ndarray, profile: CleanProfile | None = None) -> CleanResult:
    """Execute full L1 local OpenCV cleaning pipeline on a single page image."""
    prof = profile or CleanProfile()
    img = bgr.copy()
    cropped = False
    deskew_deg = 0.0
    dewarp_px = 0.0
    fingers_removed = 0

    if prof.auto_crop:
        img, cropped = detect_and_crop_page(img)
    if prof.remove_fingers:
        img, fingers_removed = remove_edge_fingers(img)
    if prof.deskew:
        img, deskew_deg = deskew_small_angle(img)
    if prof.dewarp:
        img, dewarp_px = dewarp_curved_page(img)

    cleaned, bg_stats = flatten_illumination_and_enhance(
        img,
        enhance_mode=prof.enhance_mode,
        denoise=prof.denoise,
    )
    diagnostics = {
        "cropped": cropped,
        "deskew_deg": deskew_deg,
        "dewarp_px": dewarp_px,
        "fingers_removed": fingers_removed,
        "output_width": int(cleaned.shape[1]),
        "output_height": int(cleaned.shape[0]),
        **bg_stats,
    }
    return CleanResult(image=cleaned, diagnostics=diagnostics)
