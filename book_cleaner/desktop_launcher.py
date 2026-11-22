"""Zero-config Windows/macOS desktop system-tray launcher for SuperScanner."""

from __future__ import annotations

import argparse
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import json
import webbrowser
from pathlib import Path
from typing import Any

import uvicorn
from PIL import Image, ImageDraw

from book_cleaner.api import create_app, detect_lan_ip
from book_cleaner.config import AppConfig


def resolve_desktop_data_dir(explicit_dir: str | Path | None = None) -> Path:
    """Resolve user data directory for frozen portable, frozen installed, and dev runs."""
    if explicit_dir is not None and str(explicit_dir).strip():
        return Path(explicit_dir).expanduser().resolve()

    env_dir = os.environ.get("BOOK_CLEANER_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "portable.flag").exists():
            return (exe_dir / "data").resolve()
        if sys.platform == "win32":
            local_app = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            return (local_app / "SuperScanner" / "data").resolve()
        if sys.platform == "darwin":
            return (Path.home() / "Library" / "Application Support" / "SuperScanner" / "data").resolve()
        return (Path.home() / ".local" / "share" / "superscanner" / "data").resolve()

    return Path("./data").resolve()


def check_existing_instance(port: int, timeout: float = 0.5) -> bool:
    """Return True if an active SuperScanner server is already responding on 127.0.0.1:port."""
    url = f"http://127.0.0.1:{int(port)}/api/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
            return bool(isinstance(data, dict) and data.get("ok") is True)
    except Exception:
        return False


def find_available_port(preferred: int = 8765, max_tries: int = 30) -> int:
    """Find the first bindable localhost TCP port starting from `preferred`."""
    base = max(1024, int(preferred))
    for offset in range(max(1, int(max_tries))):
        candidate = base + offset
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", candidate))
            return candidate
        except OSError:
            continue
        finally:
            sock.close()
    raise RuntimeError(f"未能找到可用本地端口（已尝试 {base}~{base + max_tries - 1}）")


def build_tray_icon_image(size: int = 64) -> Image.Image:
    """Render a crisp Studio monochrome aperture icon matching the SuperScanner badge."""
    s = max(16, int(size))
    scale = 4
    hi = s * scale
    img = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = int(hi * 0.06)
    draw.ellipse([pad, pad, hi - pad, hi - pad], fill=(24, 24, 27, 255))

    cx = hi / 2.0
    cy = hi / 2.0
    r_outer = hi * 0.34
    r_inner = hi * 0.14
    lw = max(2, int(hi * 0.045))

    # Outer lens ring & aperture blades
    draw.ellipse(
        [cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer],
        outline=(250, 250, 250, 255),
        width=lw,
    )
    for idx in range(6):
        ang = math.radians(idx * 60 + 18)
        x0 = cx + r_inner * math.cos(ang)
        y0 = cy + r_inner * math.sin(ang)
        ang2 = ang + math.radians(72)
        x1 = cx + r_outer * 0.94 * math.cos(ang2)
        y1 = cy + r_outer * 0.94 * math.sin(ang2)
        draw.line([x0, y0, x1, y1], fill=(250, 250, 250, 235), width=max(2, lw - 1))

    return img.resize((s, s), Image.Resampling.LANCZOS)


def save_app_icon(out_path: Path | str) -> Path:
    """Generate a multi-resolution `.ico` file for PyInstaller & Inno Setup installers."""
    target = Path(out_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    master = build_tray_icon_image(size=256)
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(str(target), format="ICO", sizes=sizes)
    return target


def open_data_directory(data_dir: Path | str) -> None:
    """Open the local workspace data folder in the OS native file explorer."""
    path = Path(data_dir).resolve()
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class DesktopLauncher:
    """Coordinates embedded FastAPI + Uvicorn server, browser open, and OS tray icon."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 8765,
        data_dir: Path | str | None = None,
        open_browser: bool = True,
        use_tray: bool = True,
    ) -> None:
        self.host = host
        self.preferred_port = int(port)
        self.data_dir = resolve_desktop_data_dir(data_dir)
        self.open_browser = bool(open_browser)
        self.use_tray = bool(use_tray)

        self.port = self.preferred_port
        self._server: uvicorn.Server | None = None
        self._server_thread: threading.Thread | None = None
        self._tray_icon: Any = None
        self._stop_event = threading.Event()

    @property
    def workbench_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def mobile_url(self) -> str:
        lan_ip = detect_lan_ip()
        return f"http://{lan_ip}:{self.port}/mobile"

    def _wait_ready(self, timeout_s: float = 15.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline and not self._stop_event.is_set():
            if check_existing_instance(self.port, timeout=0.35):
                return True
            time.sleep(0.15)
        return False

    def stop(self) -> None:
        self._stop_event.set()
        if self._server is not None:
            self._server.should_exit = True
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        if self._server_thread is not None and self._server_thread.is_alive():
            self._server_thread.join(timeout=4.0)

    def _run_tray(self) -> bool:
        if not self.use_tray:
            return False
        try:
            import pystray  # type: ignore[import-not-found]
        except Exception:
            return False

        icon_img = build_tray_icon_image(64)
        menu = pystray.Menu(
            pystray.MenuItem("打开工作台", lambda icon, item: webbrowser.open(self.workbench_url), default=True),
            pystray.MenuItem("打开手机扫码地址", lambda icon, item: webbrowser.open(self.mobile_url)),
            pystray.MenuItem("查看数据目录", lambda icon, item: open_data_directory(self.data_dir)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("安全退出", lambda icon, item: self.stop()),
        )
        self._tray_icon = pystray.Icon(
            "SuperScanner",
            icon_img,
            f"超级扫描仪 · SuperScanner ({self.workbench_url})",
            menu,
        )
        self._tray_icon.run()
        return True

    def run(self) -> int:
        # If an instance is already running on the preferred port, focus/open browser and exit cleanly.
        if check_existing_instance(self.preferred_port):
            self.port = self.preferred_port
            if self.open_browser:
                webbrowser.open(self.workbench_url)
            print(f"超级扫描仪 · SuperScanner 已在运行 -> {self.workbench_url}")
            return 0

        self.port = find_available_port(self.preferred_port)
        cfg = AppConfig(data_dir=self.data_dir, host=self.host, port=self.port)
        cfg.ensure_dirs()
        app = create_app(cfg, start_workers=True)

        u_cfg = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(u_cfg)
        self._server_thread = threading.Thread(target=self._server.run, name="superscanner-http", daemon=True)
        self._server_thread.start()

        self._wait_ready(timeout_s=15.0)
        print(f"超级扫描仪 · SuperScanner -> {self.workbench_url}")
        print(f"手机扫码直传地址         -> {self.mobile_url}")
        print(f"本地数据存储目录         -> {self.data_dir}")

        if self.open_browser:
            webbrowser.open(self.workbench_url)

        def _handle_sig(_sig: int, _frame: Any) -> None:
            self.stop()

        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is not None:
                try:
                    signal.signal(sig, _handle_sig)
                except Exception:
                    pass

        ran_tray = self._run_tray()
        if not ran_tray:
            try:
                while not self._stop_event.is_set() and self._server_thread.is_alive():
                    self._server_thread.join(timeout=0.5)
            except KeyboardInterrupt:
                self.stop()

        self.stop()
        return 0


def run_desktop_launcher(
    *,
    host: str = "0.0.0.0",
    port: int = 8765,
    data_dir: Path | str | None = None,
    open_browser: bool = True,
    use_tray: bool = True,
) -> int:
    """Convenience entry for launching the desktop tray server."""
    launcher = DesktopLauncher(
        host=host,
        port=port,
        data_dir=data_dir,
        open_browser=open_browser,
        use_tray=use_tray,
    )
    return launcher.run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="superscanner-desktop",
        description="超级扫描仪 · SuperScanner 一键离线桌面端与系统托盘启动器",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument(
        "--export-icon",
        type=Path,
        default=None,
        help="Export multi-resolution .ico icon and exit without starting server",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.export_icon is not None:
        out = save_app_icon(args.export_icon)
        print(f"Exported desktop icon -> {out}")
        return 0
    return run_desktop_launcher(
        host=args.host,
        port=args.port,
        data_dir=args.data_dir,
        open_browser=not args.no_browser,
        use_tray=not args.no_tray,
    )


if __name__ == "__main__":
    raise SystemExit(main())
