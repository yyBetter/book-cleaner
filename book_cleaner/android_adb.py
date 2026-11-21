"""Android USB ADB reverse-port tunnel and direct mobile Chrome launcher."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


def find_adb_executable() -> str | None:
    """Locate adb on PATH or common Windows/macOS Android SDK installation directories."""
    found = shutil.which("adb")
    if found:
        return found

    candidates: list[Path] = []
    local_app = os.environ.get("LOCALAPPDATA", "")
    user_home = Path.home()
    if local_app:
        candidates.append(Path(local_app) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
        candidates.append(Path(local_app) / "Microsoft" / "WinGet" / "Links" / "adb.exe")
    candidates.extend(
        [
            user_home / "scoop" / "shims" / "adb.exe",
            Path("C:/platform-tools/adb.exe"),
            Path("C:/ProgramData/chocolatey/bin/adb.exe"),
            user_home / "Library" / "Android" / "sdk" / "platform-tools" / "adb",
            Path("/opt/homebrew/bin/adb"),
        ]
    )
    for c in candidates:
        if c.exists() and c.is_file():
            return str(c)
    return None


def _run_adb(adb_path: str, args: list[str], timeout_s: float = 8.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [adb_path, *args],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def get_adb_status(port: int = 8765) -> dict[str, Any]:
    """Report ADB availability and attached Android devices."""
    adb_path = find_adb_executable()
    usb_url = f"http://127.0.0.1:{int(port)}/mobile"
    if not adb_path:
        return {
            "adb_available": False,
            "adb_path": "",
            "devices": [],
            "usb_url": usb_url,
            "message": "未检测到 adb 工具（Wi-Fi HTTPS 模式无需 adb，或可安装 Android platform-tools 启用 USB 极速通道）",
        }

    try:
        proc = _run_adb(adb_path, ["devices", "-l"], timeout_s=5.0)
        devices: list[dict[str, str]] = []
        for line in proc.stdout.splitlines()[1:]:
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split()
            serial = parts[0]
            state = parts[1] if len(parts) > 1 else "unknown"
            model = ""
            for token in parts[2:]:
                if token.startswith("model:"):
                    model = token.split(":", 1)[1]
            devices.append({"serial": serial, "state": state, "model": model or serial})

        ready_devices = [d for d in devices if d["state"] == "device"]
        if not devices:
            msg = "已找到 adb，请用 USB 数据线连接 Android 手机并开启「USB 调试」"
        elif not ready_devices:
            msg = "检测到 Android 设备但尚未授权，请在手机弹窗点击「允许 USB 调试」"
        else:
            msg = f"已连接 {len(ready_devices)} 台 Android 设备，可一键开启 USB 免证书高清连拍"

        return {
            "adb_available": True,
            "adb_path": adb_path,
            "devices": devices,
            "ready_count": len(ready_devices),
            "usb_url": usb_url,
            "message": msg,
        }
    except Exception as exc:
        return {
            "adb_available": True,
            "adb_path": adb_path,
            "devices": [],
            "ready_count": 0,
            "usb_url": usb_url,
            "message": f"ADB 查询失败: {exc}",
        }


def setup_adb_reverse(
    port: int = 8765,
    book_id: int | None = None,
    open_on_phone: bool = True,
) -> dict[str, Any]:
    """Bind `adb reverse tcp:<port> tcp:<port>` and optionally open mobile page on Android."""
    adb_path = find_adb_executable()
    if not adb_path:
        return {"ok": False, "message": "未检测到 adb，请安装 Android platform-tools 或改用 Wi-Fi HTTPS 扫码"}

    status = get_adb_status(port=port)
    ready = [d for d in status.get("devices", []) if d.get("state") == "device"]
    if not ready:
        return {"ok": False, "message": status.get("message", "未找到已授权的 Android 设备"), "devices": status.get("devices", [])}

    q = f"?book_id={int(book_id)}" if book_id is not None else ""
    target_url = f"http://127.0.0.1:{int(port)}/mobile{q}"

    results: list[str] = []
    for dev in ready:
        serial = dev["serial"]
        rev = _run_adb(adb_path, ["-s", serial, "reverse", f"tcp:{int(port)}", f"tcp:{int(port)}"])
        if rev.returncode != 0:
            return {"ok": False, "message": f"adb reverse 失败 ({serial}): {rev.stderr.strip() or rev.stdout.strip()}"}
        if open_on_phone:
            _run_adb(
                adb_path,
                ["-s", serial, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", target_url],
            )
        results.append(dev.get("model") or serial)

    return {
        "ok": True,
        "usb_url": target_url,
        "devices": results,
        "message": f"USB 通道已打通 ({', '.join(results)})，手机已跳转至 {target_url}",
    }
