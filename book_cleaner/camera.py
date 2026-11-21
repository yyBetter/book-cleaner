"""Desktop USB/Continuity camera manager, Windows DirectShow support, and hands-on zone-aware auto-trigger."""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Generator

import cv2
import numpy as np

from book_cleaner.pipeline.qa import compute_dhash, hamming_distance


_STATUS_LABELS = {
    "IDLE": "等待翻页（双手可压住左右白边）",
    "HAND_IN_CENTER": "手在正文区，撤回白边后自动抓拍",
    "MOTION_DETECTED": "检测到翻页动作...",
    "SETTLING": "画面稳住，优选最清晰帧...",
    "COOLDOWN": "已抓拍入库，请翻下一页",
}


def _create_video_capture(index: int) -> cv2.VideoCapture:
    """Open camera with DirectShow + MJPG fast path on Windows."""
    idx = int(index)
    if sys.platform == "win32":
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if cap is not None and cap.isOpened():
            try:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            except Exception:
                pass
            return cap
        if cap is not None:
            cap.release()
    return cv2.VideoCapture(idx)


@dataclass(slots=True)
class AutoTriggerConfig:
    enabled: bool = False
    book_id: int | None = None
    hands_on_assist: bool = True
    margin_ratio: float = 0.16
    motion_threshold: float = 10.5
    settle_threshold: float = 3.2
    settle_ms: int = 380
    cooldown_ms: int = 1200
    center_skin_max_ratio: float = 0.035
    page_change_hamming: int = 7


class AutoTriggerState:
    """Zone-aware state machine supporting hands-on-page margin holding without foot pedals."""

    def __init__(self) -> None:
        self.config = AutoTriggerConfig()
        self.state: str = "IDLE"
        self.status_label: str = _STATUS_LABELS["IDLE"]
        self.motion_score: float = 0.0
        self.center_skin_ratio: float = 0.0
        self.settle_progress: float = 0.0
        self._prev_roi_gray: np.ndarray | None = None
        self._settle_started_at: float = 0.0
        self._cooldown_started_at: float = 0.0
        self.last_trigger_at: float = 0.0
        self.last_captured_dhash: str = ""
        self.trigger_count: int = 0
        self._best_sharpness: float = -1.0
        self._best_frame: np.ndarray | None = None

    def reset(self, *, clear_last_hash: bool = False) -> None:
        self.state = "IDLE"
        self.status_label = _STATUS_LABELS["IDLE"]
        self.motion_score = 0.0
        self.center_skin_ratio = 0.0
        self.settle_progress = 0.0
        self._prev_roi_gray = None
        self._settle_started_at = 0.0
        self._cooldown_started_at = 0.0
        self._best_sharpness = -1.0
        self._best_frame = None
        if clear_last_hash:
            self.last_captured_dhash = ""

    def _extract_center_roi(self, small_bgr: np.ndarray) -> np.ndarray:
        if not self.config.hands_on_assist:
            return small_bgr
        h, w = small_bgr.shape[:2]
        m = max(0.0, min(0.30, float(self.config.margin_ratio)))
        x0 = int(round(w * m))
        x1 = max(x0 + 10, int(round(w * (1.0 - m))))
        y0 = int(round(h * 0.08))
        y1 = max(y0 + 10, int(round(h * 0.92)))
        return small_bgr[y0:y1, x0:x1]

    @staticmethod
    def _compute_skin_ratio(roi_bgr: np.ndarray) -> float:
        ycrcb = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2YCrCb)
        hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
        y_ch, cr_ch, cb_ch = cv2.split(ycrcb)
        h_ch, s_ch, v_ch = cv2.split(hsv)
        skin_ycrcb = (
            (cr_ch >= 139)
            & (cr_ch <= 180)
            & (cb_ch >= 75)
            & (cb_ch <= 128)
            & (y_ch >= 50)
            & (y_ch <= 235)
        )
        skin_hsv = (s_ch >= 28) & (s_ch <= 210) & (v_ch >= 55) & ((h_ch <= 20) | (h_ch >= 165))
        return float(np.mean(skin_ycrcb & skin_hsv))

    def mark_manual_capture(self, frame_bgr: np.ndarray) -> None:
        """Sync last captured center dHash after a manual shutter click."""
        small = cv2.resize(frame_bgr, (200, 150), interpolation=cv2.INTER_AREA)
        roi_bgr = self._extract_center_roi(small)
        self.last_captured_dhash = compute_dhash(roi_bgr)

    def pop_best_frame(self) -> np.ndarray | None:
        frame = self._best_frame
        self._best_frame = None
        self._best_sharpness = -1.0
        return frame

    def update(self, frame_bgr: np.ndarray, now: float | None = None) -> bool:
        """Feed current frame; returns True on exact transition to capture trigger."""
        ts = time.monotonic() if now is None else now
        small_bgr = cv2.resize(frame_bgr, (200, 150), interpolation=cv2.INTER_AREA)
        roi_bgr = self._extract_center_roi(small_bgr)
        roi_gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)

        if self._prev_roi_gray is None or self._prev_roi_gray.shape != roi_gray.shape:
            self._prev_roi_gray = roi_gray
            return False

        mad = float(np.mean(np.abs(roi_gray - self._prev_roi_gray)))
        self._prev_roi_gray = roi_gray
        self.motion_score = round(mad, 2)

        skin_ratio = self._compute_skin_ratio(roi_bgr) if self.config.hands_on_assist else 0.0
        self.center_skin_ratio = round(skin_ratio, 4)

        if not self.config.enabled:
            self.state = "IDLE"
            self.status_label = _STATUS_LABELS["IDLE"]
            self.settle_progress = 0.0
            return False

        if self.state == "COOLDOWN":
            self.settle_progress = 0.0
            if (ts - self._cooldown_started_at) * 1000.0 >= self.config.cooldown_ms:
                self.state = "IDLE"
                self.status_label = _STATUS_LABELS["IDLE"]
            return False

        # Block capture while hand/arm is crossing the central reading area
        if self.config.hands_on_assist and skin_ratio > self.config.center_skin_max_ratio:
            self.state = "HAND_IN_CENTER"
            self.status_label = _STATUS_LABELS["HAND_IN_CENTER"]
            self.settle_progress = 0.0
            self._best_frame = None
            self._best_sharpness = -1.0
            return False

        center_dhash = compute_dhash(roi_bgr)
        is_new_content = (
            not self.last_captured_dhash
            or hamming_distance(center_dhash, self.last_captured_dhash) >= self.config.page_change_hamming
        )

        if self.state == "IDLE":
            self.settle_progress = 0.0
            if mad >= self.config.motion_threshold:
                self.state = "MOTION_DETECTED"
                self.status_label = _STATUS_LABELS["MOTION_DETECTED"]
            elif self.last_captured_dhash and is_new_content and mad <= self.config.settle_threshold:
                # Handles slow page swaps where per-frame MAD stayed low
                self.state = "SETTLING"
                self.status_label = _STATUS_LABELS["SETTLING"]
                self._settle_started_at = ts
                self._best_sharpness = float(cv2.Laplacian(roi_gray, cv2.CV_32F).var())
                self._best_frame = frame_bgr.copy()
            return False

        if self.state in ("MOTION_DETECTED", "HAND_IN_CENTER"):
            self.settle_progress = 0.0
            if mad <= self.config.settle_threshold:
                self.state = "SETTLING"
                self.status_label = _STATUS_LABELS["SETTLING"]
                self._settle_started_at = ts
                self._best_sharpness = float(cv2.Laplacian(roi_gray, cv2.CV_32F).var())
                self._best_frame = frame_bgr.copy()
            else:
                self.state = "MOTION_DETECTED"
                self.status_label = _STATUS_LABELS["MOTION_DETECTED"]
            return False

        if self.state == "SETTLING":
            if mad >= self.config.motion_threshold:
                self.state = "MOTION_DETECTED"
                self.status_label = _STATUS_LABELS["MOTION_DETECTED"]
                self.settle_progress = 0.0
                self._best_frame = None
                self._best_sharpness = -1.0
                return False

            sharpness = float(cv2.Laplacian(roi_gray, cv2.CV_32F).var())
            if sharpness >= self._best_sharpness:
                self._best_sharpness = sharpness
                self._best_frame = frame_bgr.copy()

            elapsed_ms = (ts - self._settle_started_at) * 1000.0
            self.settle_progress = round(min(1.0, max(0.0, elapsed_ms / max(1.0, float(self.config.settle_ms)))), 2)
            if elapsed_ms >= self.config.settle_ms:
                if self.last_captured_dhash and not is_new_content:
                    self.state = "IDLE"
                    self.status_label = "页内容未变，已跳过重复拍摄"
                    self.settle_progress = 0.0
                    self._best_frame = None
                    self._best_sharpness = -1.0
                    return False

                self.last_captured_dhash = center_dhash
                self.state = "COOLDOWN"
                self.status_label = _STATUS_LABELS["COOLDOWN"]
                self.settle_progress = 1.0
                self._cooldown_started_at = ts
                self.last_trigger_at = ts
                self.trigger_count += 1
                return True

        return False


class CameraManager:
    """Thread-safe lazy OpenCV camera controller (never opens camera on startup)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cap: cv2.VideoCapture | None = None
        self._active_index: int | None = None
        self._width: int = 1920
        self._height: int = 1080
        self._latest_frame: np.ndarray | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self.auto_trigger = AutoTriggerState()
        self._on_auto_capture: Callable[[int, bytes], None] | None = None

    def set_auto_capture_callback(self, cb: Callable[[int, bytes], None] | None) -> None:
        self._on_auto_capture = cb

    def list_devices(self, max_index: int = 5) -> list[dict[str, Any]]:
        """Probe local camera indices safely without leaving devices locked."""
        devices: list[dict[str, Any]] = []
        with self._lock:
            active_idx = self._active_index
            is_open = self._cap is not None and self._cap.isOpened()

        for idx in range(max_index):
            if is_open and idx == active_idx:
                devices.append(
                    {
                        "index": idx,
                        "name": f"Camera #{idx} (Active)",
                        "active": True,
                        "width": self._width,
                        "height": self._height,
                    }
                )
                continue
            cap = _create_video_capture(idx)
            try:
                if cap is not None and cap.isOpened():
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                    devices.append(
                        {
                            "index": idx,
                            "name": f"Camera #{idx}",
                            "active": False,
                            "width": w,
                            "height": h,
                        }
                    )
            finally:
                if cap is not None:
                    cap.release()
        return devices

    def open(self, index: int = 0, width: int = 1920, height: int = 1080) -> dict[str, Any]:
        self.close()
        cap = _create_video_capture(int(index))
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"无法打开摄像头 #{index}（请检查连接或系统权限）")

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            raise RuntimeError(f"摄像头 #{index} 已连接但未能读取画面")

        actual_h, actual_w = frame.shape[:2]
        with self._lock:
            self._cap = cap
            self._active_index = int(index)
            self._width = actual_w
            self._height = actual_h
            self._latest_frame = frame
            self._running = True
            self.auto_trigger.reset(clear_last_hash=True)
            self._thread = threading.Thread(target=self._read_loop, daemon=True, name="camera-reader")
            self._thread.start()

        return self.status()

    def close(self) -> dict[str, Any]:
        with self._lock:
            self._running = False
            thread = self._thread
            self._thread = None

        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)

        with self._lock:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None
            self._active_index = None
            self._latest_frame = None
            self.auto_trigger.config.enabled = False
            self.auto_trigger.reset(clear_last_hash=True)
        return self.status()

    def _read_loop(self) -> None:
        while True:
            with self._lock:
                if not self._running or self._cap is None:
                    break
                cap = self._cap
            ok, frame = cap.read()
            if ok and frame is not None:
                best_frame = None
                with self._lock:
                    self._latest_frame = frame
                    triggered = self.auto_trigger.update(frame)
                    book_id = self.auto_trigger.config.book_id
                    cb = self._on_auto_capture
                    if triggered:
                        best_frame = self.auto_trigger.pop_best_frame()
                if triggered and book_id is not None and cb is not None:
                    chosen = best_frame if best_frame is not None else frame
                    ok_enc, buf = cv2.imencode(".jpg", chosen, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
                    if ok_enc:
                        try:
                            cb(book_id, buf.tobytes())
                        except Exception:
                            pass
            time.sleep(0.03)

    def configure_auto_trigger(
        self,
        *,
        enabled: bool,
        book_id: int | None = None,
        settle_ms: int | None = None,
        hands_on_assist: bool | None = None,
        margin_ratio: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self.auto_trigger.config.enabled = bool(enabled)
            if book_id is not None:
                self.auto_trigger.config.book_id = int(book_id)
            if settle_ms is not None:
                self.auto_trigger.config.settle_ms = max(200, min(2000, int(settle_ms)))
            if hands_on_assist is not None:
                self.auto_trigger.config.hands_on_assist = bool(hands_on_assist)
            if margin_ratio is not None:
                self.auto_trigger.config.margin_ratio = max(0.05, min(0.28, float(margin_ratio)))
            self.auto_trigger.reset(clear_last_hash=not bool(enabled))
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            is_open = self._cap is not None and self._cap.isOpened() and self._running
            return {
                "is_open": is_open,
                "active_index": self._active_index,
                "width": self._width if is_open else 0,
                "height": self._height if is_open else 0,
                "auto_trigger_enabled": self.auto_trigger.config.enabled,
                "hands_on_assist": self.auto_trigger.config.hands_on_assist,
                "margin_ratio": self.auto_trigger.config.margin_ratio,
                "auto_trigger_state": self.auto_trigger.state,
                "status_label": self.auto_trigger.status_label,
                "motion_score": self.auto_trigger.motion_score,
                "center_skin_ratio": self.auto_trigger.center_skin_ratio,
                "settle_progress": self.auto_trigger.settle_progress,
                "trigger_count": self.auto_trigger.trigger_count,
                "settle_ms": self.auto_trigger.config.settle_ms,
            }

    def get_preview_jpeg(self, max_width: int = 960) -> bytes | None:
        with self._lock:
            frame = None if self._latest_frame is None else self._latest_frame.copy()
        if frame is None:
            return None
        h, w = frame.shape[:2]
        if w > max_width and w > 0:
            scale = max_width / float(w)
            frame = cv2.resize(
                frame,
                (max_width, max(1, int(round(h * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return buf.tobytes() if ok else None

    def capture_full_frame(self) -> bytes:
        with self._lock:
            frame = None if self._latest_frame is None else self._latest_frame.copy()
            if frame is not None:
                self.auto_trigger.mark_manual_capture(frame)
        if frame is None:
            raise RuntimeError("后端摄像头未开启，请先打开摄像头或改用浏览器/手机采集")
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 94])
        if not ok:
            raise RuntimeError("摄像头帧编码失败")
        return buf.tobytes()

    def mjpeg_generator(self) -> Generator[bytes, None, None]:
        while True:
            jpg = self.get_preview_jpeg()
            if jpg is None:
                break
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                + f"Content-Length: {len(jpg)}\r\n\r\n".encode("ascii")
                + jpg
                + b"\r\n"
            )
            time.sleep(0.066)
