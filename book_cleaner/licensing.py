"""Pure-offline Ed25519 machine-code licensing for SuperScanner (zero network calls)."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


FREE_EXPORT_PAGE_LIMIT = 5
TOKEN_PREFIX = "SS1"
VALID_PAID_TIERS = ("pro", "enterprise")
ALL_TIERS = ("free", "pro", "enterprise")
TIER_LABELS: dict[str, str] = {
    "free": "免费体验版",
    "pro": "个人专业买断版",
    "enterprise": "企业多表工位版",
}

# Embedded Ed25519 public key (32 bytes, unpadded URL-safe base64).
# Run `python scripts/issue_license.py keygen --write-public-key` before commercial packaging
# to bind builds to your own private vendor key in `keys/vendor_ed25519.pem`.
EMBEDDED_PUBLIC_KEY_B64 = "t4BUDVBJ0WywPAI16IBMI6toAtrDksplKoHdgadwnI4"

# Unambiguous Crockford-like 32-char alphabet (omits I, O, 0, 1).
MACHINE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_MACHINE_CODE_RE = re.compile(r"^SS-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")

_CACHED_MACHINE_CODE: str | None = None


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(encoded: str) -> bytes:
    s = str(encoded or "").strip()
    if not s:
        raise ValueError("empty base64url segment")
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad))


def _read_hardware_identifiers() -> list[str]:
    """Collect stable host hardware identifiers on Windows, macOS, or Linux with safe fallbacks."""
    parts: list[str] = []

    if sys.platform == "win32":
        try:
            import winreg  # type: ignore[import-not-found]

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
            ) as key:
                guid, _ = winreg.QueryValueEx(key, "MachineGuid")
                if guid and str(guid).strip():
                    parts.append(f"win_guid:{str(guid).strip()}")
        except Exception:
            pass

        try:
            sys_drive = os.environ.get("SystemDrive", "C:")
            root_path = f"{sys_drive.rstrip('\\/')}\\"
            serial = ctypes.c_uint32(0)
            ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
                ctypes.c_wchar_p(root_path),
                None,
                0,
                ctypes.byref(serial),
                None,
                None,
                None,
                0,
            )
            if ok and serial.value:
                parts.append(f"win_vol:{serial.value:08x}")
        except Exception:
            pass

        try:
            res = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-CimInstance Win32_ComputerSystemProduct).UUID",
                ],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            uid = res.stdout.strip()
            if uid and uid.upper() not in ("FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF", ""):
                parts.append(f"win_smbios:{uid}")
        except Exception:
            pass

    elif sys.platform == "darwin":
        try:
            res = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', res.stdout)
            if m and m.group(1).strip():
                parts.append(f"mac_uuid:{m.group(1).strip()}")
        except Exception:
            pass

        if not parts:
            try:
                res = subprocess.run(
                    ["diskutil", "info", "/"],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                    check=False,
                )
                m = re.search(r"Volume UUID:\s*([A-Fa-f0-9-]+)", res.stdout)
                if m and m.group(1).strip():
                    parts.append(f"mac_vol:{m.group(1).strip()}")
            except Exception:
                pass

    else:
        for mid_path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                if mid_path.is_file():
                    val = mid_path.read_text(encoding="utf-8").strip()
                    if val:
                        parts.append(f"linux_mid:{val}")
                        break
            except Exception:
                pass

    if not parts:
        node = f"{uuid.getnode():012x}"
        parts.append(f"node:{node}|host:{platform.node()}|sys:{platform.system()}|arch:{platform.machine()}")

    return parts


def format_machine_code_from_seed(seed: str) -> str:
    """Derive a deterministic `SS-XXXX-XXXX-XXXX` machine code from an arbitrary hardware seed."""
    digest = hashlib.sha256(f"SuperScanner-HWID-v1:{seed}".encode("utf-8")).digest()
    chars = [MACHINE_CODE_ALPHABET[b % len(MACHINE_CODE_ALPHABET)] for b in digest[:12]]
    return f"SS-{''.join(chars[0:4])}-{''.join(chars[4:8])}-{''.join(chars[8:12])}"


def get_machine_code(*, refresh: bool = False) -> str:
    """Return this host's stable short machine code (`SS-XXXX-XXXX-XXXX`)."""
    global _CACHED_MACHINE_CODE
    if _CACHED_MACHINE_CODE is not None and not refresh:
        return _CACHED_MACHINE_CODE
    seed = "|".join(_read_hardware_identifiers())
    _CACHED_MACHINE_CODE = format_machine_code_from_seed(seed)
    return _CACHED_MACHINE_CODE


def normalize_machine_code(code: str) -> str:
    """Normalize user-entered machine code spacing/hyphens into `SS-XXXX-XXXX-XXXX`."""
    raw = re.sub(r"[\s\-_]+", "", str(code or "").strip().upper())
    if raw.startswith("SS"):
        raw = raw[2:]
    if len(raw) != 12 or any(ch not in MACHINE_CODE_ALPHABET for ch in raw):
        raise ValueError("机器码格式无效，应为 SS-XXXX-XXXX-XXXX")
    return f"SS-{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"


def is_valid_machine_code(code: str) -> bool:
    """Check whether a string conforms to canonical `SS-XXXX-XXXX-XXXX` format."""
    return bool(_MACHINE_CODE_RE.match(str(code or "").strip().upper()))


def generate_keypair() -> tuple[str, str]:
    """Generate a fresh Ed25519 private key PEM and unpadded base64url public key."""
    priv = ed25519.Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv_pem, _b64url_encode(pub_raw)


def _load_private_key(private_key_pem: str | bytes) -> ed25519.Ed25519PrivateKey:
    raw = private_key_pem.encode("utf-8") if isinstance(private_key_pem, str) else private_key_pem
    loaded = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(loaded, ed25519.Ed25519PrivateKey):
        raise TypeError("私钥必须是 Ed25519 PEM 格式")
    return loaded


def _load_public_key(public_key_b64: str) -> ed25519.Ed25519PublicKey:
    raw = _b64url_decode(public_key_b64)
    if len(raw) != 32:
        raise ValueError("Ed25519 公钥长度必须为 32 字节")
    return ed25519.Ed25519PublicKey.from_public_bytes(raw)


def issue_license_token(
    private_key_pem: str | bytes,
    *,
    machine_code: str,
    tier: str = "pro",
    expires_at: str | None = None,
    note: str = "",
    issued_at: str | None = None,
) -> str:
    """Sign an offline Ed25519 license token bound to a specific machine code."""
    tier_norm = str(tier or "").strip().lower()
    if tier_norm not in VALID_PAID_TIERS:
        raise ValueError(f"仅支持签发付费授权等级: {', '.join(VALID_PAID_TIERS)}")

    norm_mc = normalize_machine_code(machine_code)

    exp_clean: str | None = None
    if expires_at is not None and str(expires_at).strip():
        exp_str = str(expires_at).strip()
        date.fromisoformat(exp_str)
        exp_clean = exp_str

    issued_clean = (
        str(issued_at).strip()
        if issued_at and str(issued_at).strip()
        else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    date.fromisoformat(issued_clean)

    payload = {
        "v": 1,
        "machine_code": norm_mc,
        "tier": tier_norm,
        "expires_at": exp_clean,
        "issued_at": issued_clean,
        "note": str(note or "").strip(),
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    priv = _load_private_key(private_key_pem)
    signature = priv.sign(payload_bytes)
    return f"{TOKEN_PREFIX}.{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"


@dataclass(slots=True)
class LicenseStatus:
    """Resolved licensing state returned to API clients and export gatekeepers."""

    tier: str
    tier_label: str
    active: bool
    valid: bool
    machine_code: str
    expires_at: str | None
    is_perpetual: bool
    issued_at: str | None
    note: str
    reason: str
    status_message: str
    free_export_page_limit: int
    export_unlimited: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _free_status(
    *,
    machine_code: str,
    free_export_page_limit: int,
    reason: str = "free",
    status_message: str | None = None,
    expires_at: str | None = None,
    issued_at: str | None = None,
    note: str = "",
) -> LicenseStatus:
    limit = max(1, int(free_export_page_limit))
    msg = status_message or (
        f"免费体验版单次导出前 {limit} 页，输入离线激活码解锁不限页与多目标 CatBoost 全量导出"
    )
    return LicenseStatus(
        tier="free",
        tier_label=TIER_LABELS["free"],
        active=False,
        valid=(reason == "free"),
        machine_code=machine_code,
        expires_at=expires_at,
        is_perpetual=False,
        issued_at=issued_at,
        note=note,
        reason=reason,
        status_message=msg,
        free_export_page_limit=limit,
        export_unlimited=False,
    )


def verify_license_token(
    token: str,
    *,
    expected_machine_code: str | None = None,
    public_key_b64: str | None = None,
    free_export_page_limit: int = FREE_EXPORT_PAGE_LIMIT,
    now: datetime | None = None,
) -> LicenseStatus:
    """Verify an offline Ed25519 license token strictly locally (never hits network)."""
    host_mc = normalize_machine_code(expected_machine_code) if expected_machine_code else get_machine_code()
    limit = max(1, int(free_export_page_limit))
    raw = str(token or "").strip()
    if not raw:
        return _free_status(machine_code=host_mc, free_export_page_limit=limit, reason="empty")

    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return _free_status(
            machine_code=host_mc,
            free_export_page_limit=limit,
            reason="malformed",
            status_message="激活码格式不正确，请确认完整复制以 SS1. 开头的离线激活码",
        )

    try:
        payload_bytes = _b64url_decode(parts[1])
        sig_bytes = _b64url_decode(parts[2])
        payload = json.loads(payload_bytes.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ValueError("unsupported payload")
    except Exception:
        return _free_status(
            machine_code=host_mc,
            free_export_page_limit=limit,
            reason="malformed",
            status_message="激活码内容损坏或无法解析，请重新复制离线激活码",
        )

    pubkey_str = (
        public_key_b64
        or os.environ.get("BOOK_CLEANER_LICENSE_PUBKEY")
        or EMBEDDED_PUBLIC_KEY_B64
    )
    try:
        pub_key = _load_public_key(pubkey_str)
        pub_key.verify(sig_bytes, payload_bytes)
    except (InvalidSignature, ValueError):
        return _free_status(
            machine_code=host_mc,
            free_export_page_limit=limit,
            reason="invalid_signature",
            status_message="离线激活码签名校验未通过（可能被篡改或非官方签发）",
        )

    tier = str(payload.get("tier") or "").strip().lower()
    if tier not in VALID_PAID_TIERS:
        return _free_status(
            machine_code=host_mc,
            free_export_page_limit=limit,
            reason="invalid_tier",
            status_message="激活码授权等级无效",
        )

    try:
        token_mc = normalize_machine_code(str(payload.get("machine_code") or ""))
    except ValueError:
        return _free_status(
            machine_code=host_mc,
            free_export_page_limit=limit,
            reason="malformed",
            status_message="激活码中的机器码格式无效",
        )

    if token_mc != host_mc:
        return _free_status(
            machine_code=host_mc,
            free_export_page_limit=limit,
            reason="machine_mismatch",
            status_message=f"此激活码绑定机器码为 {token_mc}，与本机 {host_mc} 不匹配",
        )

    exp_raw = payload.get("expires_at")
    exp_str = str(exp_raw).strip() if exp_raw else None
    issued_str = str(payload.get("issued_at") or "").strip() or None
    note_str = str(payload.get("note") or "").strip()

    if exp_str:
        try:
            exp_date = date.fromisoformat(exp_str)
        except ValueError:
            return _free_status(
                machine_code=host_mc,
                free_export_page_limit=limit,
                reason="malformed",
                status_message="激活码有效期日期格式无效",
            )
        ref_now = now or datetime.now(timezone.utc)
        if ref_now.date() > exp_date:
            return _free_status(
                machine_code=host_mc,
                free_export_page_limit=limit,
                reason="expired",
                expires_at=exp_str,
                issued_at=issued_str,
                note=note_str,
                status_message=f"离线激活码已于 {exp_str} 到期，已自动回退为免费体验版（限前 {limit} 页导出）",
            )

    tier_label = TIER_LABELS.get(tier, tier)
    validity_txt = f"有效期至 {exp_str}" if exp_str else "永久买断授权"
    return LicenseStatus(
        tier=tier,
        tier_label=tier_label,
        active=True,
        valid=True,
        machine_code=host_mc,
        expires_at=exp_str,
        is_perpetual=(exp_str is None),
        issued_at=issued_str,
        note=note_str,
        reason="ok",
        status_message=f"{tier_label}已激活（{validity_txt}），全格式导出与多目标 CatBoost 无页数限制",
        free_export_page_limit=limit,
        export_unlimited=True,
    )


def evaluate_stored_license(
    db: Any,
    config: Any,
    *,
    now: datetime | None = None,
    machine_code: str | None = None,
) -> LicenseStatus:
    """Load persisted license token from SQLite settings and evaluate against current machine."""
    host_mc = normalize_machine_code(machine_code) if machine_code else get_machine_code()
    limit = max(1, int(getattr(config, "free_export_page_limit", FREE_EXPORT_PAGE_LIMIT)))
    pub_b64 = getattr(config, "license_public_key_b64", None)

    settings = db.get_settings() if db is not None else {}
    lic_cfg = settings.get("license") or {}
    token = str(lic_cfg.get("license_key") or "").strip() if isinstance(lic_cfg, dict) else ""
    if not token:
        return _free_status(machine_code=host_mc, free_export_page_limit=limit, reason="free")

    return verify_license_token(
        token,
        expected_machine_code=host_mc,
        public_key_b64=pub_b64,
        free_export_page_limit=limit,
        now=now,
    )
