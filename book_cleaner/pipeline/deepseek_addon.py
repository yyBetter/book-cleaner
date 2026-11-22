"""Hardware pre-check & isolated on-demand DeepSeek-OCR 2 add-on manager (zero bloat in base package)."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import psutil


DEFAULT_HF_REPO = "deepseek-ai/DeepSeek-OCR-2"
MIN_VRAM_MB = 8192
RECOMMENDED_VRAM_MB = 12288
MIN_DISK_GB = 12.0
MIN_RAM_GB = 16.0

_WORKER_RUNNER_CODE = '''"""Isolated DeepSeek-OCR 2 inference runner executed inside add-on .venv."""
import argparse
import json
import time
from pathlib import Path

import torch
from transformers import AutoModel, AutoTokenizer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    t0 = time.perf_counter()
    model_path = str(Path(args.model_dir).resolve())
    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    model = AutoModel.from_pretrained(
        model_path,
        trust_remote_code=True,
        use_safetensors=True,
        torch_dtype=dtype,
    ).eval().cuda()

    prompt = "<image>\\n<|grounding|>Convert the document to markdown."
    result = model.infer(
        tokenizer,
        prompt=prompt,
        image_file=str(Path(args.image).resolve()),
        output_path=str(Path(args.out_dir).resolve()),
        base_size=1024,
        image_size=768,
        crop_mode=True,
        save_results=False,
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    print(json.dumps({"ok": True, "text": text, "elapsed_ms": elapsed_ms}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
'''


def _venv_python_path(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _existing_parent(path: Path) -> Path:
    cur = Path(path).resolve()
    while not cur.exists() and cur.parent != cur:
        cur = cur.parent
    return cur


def probe_nvidia_gpus() -> list[dict[str, Any]]:
    """Probe NVIDIA CUDA GPUs via `nvidia-smi` without requiring PyTorch."""
    smi = shutil.which("nvidia-smi")
    if not smi and sys.platform == "win32":
        win_cand = Path("C:/Windows/System32/nvidia-smi.exe")
        if win_cand.exists():
            smi = str(win_cand)
    if not smi:
        return []

    try:
        proc = subprocess.run(
            [smi, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if proc.returncode != 0:
            return []
        gpus: list[dict[str, Any]] = []
        for line in proc.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            try:
                vram_mb = int(float(parts[1]))
            except ValueError:
                vram_mb = 0
            gpus.append(
                {
                    "name": parts[0],
                    "vram_mb": vram_mb,
                    "driver_version": parts[2] if len(parts) > 2 else "",
                }
            )
        return gpus
    except Exception:
        return []


def inspect_hardware_for_deepseek(
    addon_root: Path,
    *,
    gpu_probe: Callable[[], list[dict[str, Any]]] | None = None,
    disk_probe: Callable[[Path], float] | None = None,
    ram_probe: Callable[[], float] | None = None,
    uv_probe: Callable[[], str | None] | None = None,
    platform_system: str | None = None,
) -> dict[str, Any]:
    """Check whether the host PC can download and run DeepSeek-OCR 2 locally."""
    sys_name = platform_system or platform.system()
    gpus = (gpu_probe or probe_nvidia_gpus)()
    max_vram_mb = max((int(g.get("vram_mb", 0)) for g in gpus), default=0)

    if disk_probe is not None:
        free_disk_gb = float(disk_probe(addon_root))
    else:
        target = _existing_parent(addon_root)
        free_disk_gb = round(shutil.disk_usage(target).free / float(1024**3), 2)

    if ram_probe is not None:
        ram_gb = float(ram_probe())
    else:
        ram_gb = round(psutil.virtual_memory().total / float(1024**3), 2)

    uv_bin = (uv_probe or (lambda: shutil.which("uv")))()

    checks = {
        "gpu": len(gpus) > 0,
        "vram": max_vram_mb >= MIN_VRAM_MB,
        "ram": ram_gb >= MIN_RAM_GB * 0.875,
        "disk": free_disk_gb >= MIN_DISK_GB,
        "uv": bool(uv_bin),
    }

    reasons: list[str] = []
    if not checks["gpu"]:
        if sys_name == "Darwin":
            reasons.append("当前为 macOS（无 NVIDIA CUDA 显卡），无法运行 CUDA 版 DeepSeek-OCR 2 本地包")
        else:
            reasons.append("未检测到 NVIDIA 独立显卡（nvidia-smi 不可用）")
    elif not checks["vram"]:
        reasons.append(f"显存不足：当前最大显存 {max_vram_mb} MB，本地运行至少需要 {MIN_VRAM_MB} MB (8 GB)")

    if not checks["ram"]:
        reasons.append(f"系统内存偏低：当前 {ram_gb:.1f} GB，建议至少 {MIN_RAM_GB:.0f} GB")
    if not checks["disk"]:
        reasons.append(f"可用磁盘空间不足：当前仅 {free_disk_gb:.1f} GB，下载权重与隔离环境需预留 >= {MIN_DISK_GB:.0f} GB")
    if not checks["uv"]:
        reasons.append("未找到 uv 包管理器（无法自动创建隔离依赖环境）")

    can_run_local = all(checks.values())
    if can_run_local:
        if max_vram_mb >= RECOMMENDED_VRAM_MB:
            recommendation = f"检测到 {gpus[0]['name']} ({max_vram_mb} MB 显存)，硬件充裕，可直接点击下载本地插件包。"
        else:
            recommendation = f"检测到 {gpus[0]['name']} ({max_vram_mb} MB 显存)，满足最低要求，可下载运行或配合混合模式使用。"
    else:
        recommendation = "当前电脑不建议本地运行 DeepSeek-OCR 2，可继续免费使用内置 RapidOCR，或配置 DeepSeek-OCR 2 视觉 API。"

    return {
        "can_run_local": can_run_local,
        "status": "eligible" if can_run_local else "insufficient_hardware",
        "platform": sys_name,
        "gpus": gpus,
        "max_vram_mb": max_vram_mb,
        "ram_gb": round(ram_gb, 2),
        "free_disk_gb": round(free_disk_gb, 2),
        "checks": checks,
        "reasons": reasons,
        "recommendation": recommendation,
    }


def _dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = 0
    for root, _, files in os.walk(path):
        for fn in files:
            try:
                total += (Path(root) / fn).stat().st_size
            except OSError:
                pass
    return round(total / float(1024 * 1024), 1)


class _PausedInstallError(Exception):
    """Raised internally when user pauses installation to keep partial files."""


class DeepSeekAddonManager:
    """Manages isolated hardware check, download, install, pause/resume, uninstall, and subprocess inference."""

    def __init__(self, data_dir: Path | str, events: Any | None = None) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.addon_root = self.data_dir / "addons"
        self.addon_dir = self.addon_root / "deepseek_ocr2"
        self.venv_dir = self.addon_dir / ".venv"
        self.model_dir = self.addon_dir / "models" / "DeepSeek-OCR-2"
        self.runner_script = self.addon_dir / "worker_runner.py"
        self.manifest_path = self.addon_dir / "manifest.json"
        self.events = events

        self._lock = threading.Lock()
        self._install_state: str = "installed" if self.is_installed() else "not_installed"
        self._stage: str = "idle"
        self._percent: int = 100 if self.is_installed() else 0
        self._message: str = "插件包已就绪" if self.is_installed() else "尚未安装（按需下载）"
        self._log_tail: str = ""
        self._active_proc: subprocess.Popen[str] | None = None
        self._cancel_requested: bool = False
        self._pause_requested: bool = False
        self._worker_thread: threading.Thread | None = None

    def is_installed(self) -> bool:
        py_exe = _venv_python_path(self.venv_dir)
        return (
            self.manifest_path.exists()
            and self.runner_script.exists()
            and py_exe.exists()
            and self.model_dir.exists()
        )

    def _emit(self, status_dict: dict[str, Any] | None = None) -> None:
        if self.events is not None:
            self.events.publish("addon_progress", status_dict or self.get_status())

    def get_status(
        self,
        *,
        hardware_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        hw = hardware_override or inspect_hardware_for_deepseek(self.addon_root)
        installed = self.is_installed()
        with self._lock:
            state = self._install_state
            if installed:
                state = "installed"
            elif state not in ("installing", "paused", "failed"):
                state = "not_installed"
            return {
                "addon_id": "deepseek_ocr2",
                "name": "DeepSeek-OCR 2 本地插件包",
                "installed": installed,
                "install_state": state,
                "can_select_local": installed,
                "stage": self._stage,
                "percent": 100 if installed and state == "installed" else self._percent,
                "message": self._message,
                "log_tail": self._log_tail,
                "addon_dir": str(self.addon_dir),
                "disk_usage_mb": _dir_size_mb(self.addon_dir),
                "hardware": hw,
            }

    def _set_progress(self, state: str, stage: str, percent: int, message: str, log_tail: str = "") -> None:
        with self._lock:
            self._install_state = state
            self._stage = stage
            self._percent = int(percent)
            self._message = message
            if log_tail:
                self._log_tail = log_tail[-400:]
        self._emit()

    def _run_step(self, cmd: list[str], env: dict[str, str] | None = None) -> None:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        with self._lock:
            self._active_proc = proc
        tail_lines: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            s = line.strip()
            if s:
                tail_lines.append(s)
                if len(tail_lines) > 8:
                    tail_lines.pop(0)
                with self._lock:
                    self._log_tail = "\n".join(tail_lines)
            if self._pause_requested or self._cancel_requested:
                proc.terminate()
                break
        rc = proc.wait()
        with self._lock:
            self._active_proc = None
        if self._pause_requested:
            raise _PausedInstallError("用户已暂停下载")
        if self._cancel_requested:
            raise InterruptedError("用户已取消安装")
        if rc != 0:
            raise RuntimeError(f"命令失败 (code={rc}): {' '.join(cmd[:3])}\n" + "\n".join(tail_lines[-3:]))

    def start_install(
        self,
        *,
        use_cn_mirror: bool = True,
        repo_id: str = DEFAULT_HF_REPO,
        force: bool = False,
        hardware_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        hw = hardware_override or inspect_hardware_for_deepseek(self.addon_root)
        if not hw["can_run_local"] and not force:
            raise ValueError("硬件检测未通过：" + "；".join(hw["reasons"]))

        with self._lock:
            if self._install_state == "installing":
                return self.get_status(hardware_override=hw)
            was_paused = self._install_state == "paused"
            self._cancel_requested = False
            self._pause_requested = False
            self._install_state = "installing"
            self._stage = "preparing"
            self._percent = max(5, self._percent if was_paused else 5)
            self._message = "正在从断点恢复隔离环境与权重下载..." if was_paused else "正在准备独立隔离环境..."
            if not was_paused:
                self._log_tail = ""

        def _installer() -> None:
            try:
                uv_bin = shutil.which("uv")
                if not uv_bin:
                    raise RuntimeError("未找到 uv 命令")
                self.addon_dir.mkdir(parents=True, exist_ok=True)
                self.model_dir.mkdir(parents=True, exist_ok=True)

                env = dict(os.environ)
                env["UV_HTTP_TIMEOUT"] = "120"
                env["UV_LINK_MODE"] = "copy"
                if use_cn_mirror:
                    env["UV_PYTHON_INSTALL_MIRROR"] = (
                        "https://registry.npmmirror.com/-/binary/python-build-standalone"
                    )
                    env["UV_DEFAULT_INDEX"] = "https://pypi.tuna.tsinghua.edu.cn/simple"
                    env["HF_ENDPOINT"] = "https://hf-mirror.com"

                py_exe = _venv_python_path(self.venv_dir)
                # 1. Create isolated .venv inside data/addons/deepseek_ocr2/ (skip wipe if resuming)
                if not was_paused or not py_exe.exists():
                    self._set_progress("installing", "venv", 15, "正在创建插件专属 Python 3.12 隔离环境...")
                    self._run_step([uv_bin, "venv", str(self.venv_dir), "--python", "3.12", "--clear"], env=env)
                    py_exe = _venv_python_path(self.venv_dir)
                # 2. Install CUDA PyTorch & runtime libs ONLY inside add-on venv
                self._set_progress("installing", "deps", 35, "正在下载安装 PyTorch CUDA 与推理依赖...")
                torch_cmd_base = [
                    uv_bin,
                    "pip",
                    "install",
                    "--python",
                    str(py_exe),
                    "torch",
                    "torchvision",
                    "--index-url",
                ]
                if use_cn_mirror:
                    try:
                        self._run_step(
                            [*torch_cmd_base, "https://mirror.nju.edu.cn/pytorch/whl/cu124"],
                            env=env,
                        )
                    except RuntimeError:
                        self._set_progress(
                            "installing",
                            "deps",
                            40,
                            "南大 PyTorch 镜像不可用，自动回退官方 CUDA 源...",
                        )
                        self._run_step(
                            [*torch_cmd_base, "https://download.pytorch.org/whl/cu124"],
                            env=env,
                        )
                else:
                    self._run_step(
                        [*torch_cmd_base, "https://download.pytorch.org/whl/cu124"],
                        env=env,
                    )

                self._set_progress("installing", "deps_hf", 55, "正在安装 Transformers 与模型下载组件...")
                self._run_step(
                    [
                        uv_bin,
                        "pip",
                        "install",
                        "--python",
                        str(py_exe),
                        "transformers>=4.46.0",
                        "accelerate",
                        "huggingface-hub>=0.26.0",
                        "pillow",
                        "einops",
                        "easydict",
                        "addict",
                    ],
                    env=env,
                )

                # 3. Download DeepSeek-OCR-2 weights
                self._set_progress("installing", "weights", 70, f"正在拉取 {repo_id} 模型权重...")
                dl_code = (
                    "from huggingface_hub import snapshot_download; "
                    f"snapshot_download(repo_id={repo_id!r}, local_dir={str(self.model_dir)!r})"
                )
                self._run_step([str(py_exe), "-c", dl_code], env=env)

                # 4. Write runner & manifest
                self.runner_script.write_text(_WORKER_RUNNER_CODE, encoding="utf-8")
                self.manifest_path.write_text(
                    json.dumps(
                        {
                            "addon_id": "deepseek_ocr2",
                            "repo_id": repo_id,
                            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "use_cn_mirror": use_cn_mirror,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                self._set_progress("installed", "done", 100, "DeepSeek-OCR 2 插件包安装完成，可切换使用")
            except _PausedInstallError:
                with self._lock:
                    cur_pct = self._percent
                self._set_progress("paused", "paused", cur_pct, "已暂停下载安装（保留断点，可随时继续）")
            except InterruptedError:
                if not self.is_installed() and self.addon_dir.exists():
                    shutil.rmtree(self.addon_dir, ignore_errors=True)
                self._set_progress("not_installed", "cancelled", 0, "已取消下载并清理未完成文件")
            except Exception as exc:
                self._set_progress("failed", "error", 0, f"安装失败: {exc}")

        self._worker_thread = threading.Thread(target=_installer, daemon=True, name="deepseek-addon-install")
        self._worker_thread.start()
        return self.get_status(hardware_override=hw)

    def pause_install(self) -> dict[str, Any]:
        with self._lock:
            self._pause_requested = True
            proc = self._active_proc
            if self._install_state == "installing" or (not self.is_installed() and self.addon_dir.exists()):
                self._install_state = "paused"
                self._stage = "paused"
                self._message = "已暂停下载安装（保留断点，可随时继续）"
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        self._emit()
        return self.get_status()

    def cancel_install(self) -> dict[str, Any]:
        with self._lock:
            self._pause_requested = False
            self._cancel_requested = True
            proc = self._active_proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)
        if not self.is_installed() and self.addon_dir.exists():
            shutil.rmtree(self.addon_dir, ignore_errors=True)
        self._set_progress("not_installed", "cancelled", 0, "已取消下载安装")
        return self.get_status()

    def uninstall(self) -> dict[str, Any]:
        self.cancel_install()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)
        if self.addon_dir.exists():
            shutil.rmtree(self.addon_dir, ignore_errors=True)
        self._set_progress("not_installed", "idle", 0, "已卸载并释放全部插件磁盘空间")
        return self.get_status()

    def run_local_inference(self, image_path: Path, timeout_s: float = 90.0) -> str:
        if not self.is_installed():
            raise RuntimeError("DeepSeek-OCR 2 本地插件包尚未安装")
        py_exe = _venv_python_path(self.venv_dir)
        out_dir = self.addon_dir / "tmp_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [
                str(py_exe),
                str(self.runner_script),
                "--image",
                str(image_path),
                "--model-dir",
                str(self.model_dir),
                "--out-dir",
                str(out_dir),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}")
        for line in reversed(proc.stdout.splitlines()):
            s = line.strip()
            if s.startswith("{"):
                data = json.loads(s)
                if data.get("ok"):
                    return str(data.get("text", "")).strip()
        raise RuntimeError("本地插件未返回有效识别 JSON")
