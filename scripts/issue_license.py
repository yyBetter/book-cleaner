"""Offline Ed25519 license keypair & buyer activation code issuer for SuperScanner."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from book_cleaner.licensing import (  # noqa: E402
    VALID_PAID_TIERS,
    generate_keypair,
    get_machine_code,
    issue_license_token,
    verify_license_token,
)

DEFAULT_PRIVATE_KEY_PATH = ROOT_DIR / "keys" / "vendor_ed25519.pem"
LICENSING_MODULE_PATH = ROOT_DIR / "book_cleaner" / "licensing.py"


def _write_embedded_public_key(pub_b64: str, target_path: Path = LICENSING_MODULE_PATH) -> None:
    content = target_path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'EMBEDDED_PUBLIC_KEY_B64\s*=\s*"[^"]*"',
        f'EMBEDDED_PUBLIC_KEY_B64 = "{pub_b64}"',
        content,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"未能在 {target_path} 中找到 EMBEDDED_PUBLIC_KEY_B64 定义")
    target_path.write_text(updated, encoding="utf-8")


def _cmd_keygen(args: argparse.Namespace) -> int:
    out_priv: Path = Path(args.out_private).resolve()
    if out_priv.exists() and not args.force:
        print(f"[错误] 私钥文件已存在：{out_priv}（如需覆盖请追加 --force）", file=sys.stderr)
        return 1

    priv_pem, pub_b64 = generate_keypair()
    out_priv.parent.mkdir(parents=True, exist_ok=True)
    out_priv.write_text(priv_pem, encoding="utf-8")
    try:
        os.chmod(out_priv, 0o600)
    except OSError:
        pass

    print(f"[成功] 已生成 Ed25519 商业签发私钥 -> {out_priv}")
    print(f"[公钥] Base64URL Public Key: {pub_b64}")
    if args.write_public_key:
        _write_embedded_public_key(pub_b64)
        print(f"[成功] 已将公钥自动写入 -> {LICENSING_MODULE_PATH}")
    else:
        print("提示：追加 --write-public-key 可自动同步公钥到 book_cleaner/licensing.py。")
    print("警告：keys/ 目录已被 .gitignore 忽略，请妥善离线备份私钥，切勿随软件分发！")
    return 0


def _cmd_issue(args: argparse.Namespace) -> int:
    priv_path = Path(args.private_key).resolve()
    if not priv_path.is_file():
        print(f"[错误] 未找到签发私钥：{priv_path}。请先执行 keygen 子命令。", file=sys.stderr)
        return 1

    priv_pem = priv_path.read_text(encoding="utf-8")
    expires_val = None if not args.expires or args.expires.lower() in ("none", "perpetual") else args.expires
    token = issue_license_token(
        priv_pem,
        machine_code=args.machine_code,
        tier=args.tier,
        expires_at=expires_val,
        note=args.note or "",
    )

    if args.json:
        print(
            json.dumps(
                {
                    "machine_code": args.machine_code,
                    "tier": args.tier,
                    "expires_at": expires_val,
                    "note": args.note or "",
                    "license_key": token,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    tier_name = "企业多表工位版" if args.tier == "enterprise" else "个人专业买断版"
    exp_text = f"有效期至 {expires_val}" if expires_val else "永久买断授权"
    print("=== 超级扫描仪 · SuperScanner 离线激活码已签发 ===")
    print(f"绑定机器码: {args.machine_code}")
    print(f"授权等级  : {tier_name} ({args.tier}) · {exp_text}")
    if args.note:
        print(f"签发备注  : {args.note}")
    print("")
    print(token)
    print("")
    print("--- 可直接发送给买家的发货回复模板 ---")
    print(
        f"您好！您的「超级扫描仪 · SuperScanner」{tier_name}离线激活码（{exp_text}）已生成：\n"
        f"激活码：{token}\n"
        "使用方式：打开软件右上角「授权与激活」-> 粘贴上方完整激活码 -> 点击「立即离线激活」即可永久断网使用。"
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    mc = args.machine_code or get_machine_code()
    st = verify_license_token(
        args.token,
        expected_machine_code=mc,
        public_key_b64=args.public_key_b64 or None,
    )
    print(json.dumps(st.to_dict(), ensure_ascii=False, indent=2))
    return 0 if st.valid and st.active else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issue_license",
        description="SuperScanner offline Ed25519 license keypair generator and token issuer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_kg = sub.add_parser("keygen", help="Generate Ed25519 signing keypair")
    p_kg.add_argument("--out-private", default=str(DEFAULT_PRIVATE_KEY_PATH))
    p_kg.add_argument("--force", action="store_true")
    p_kg.add_argument("--write-public-key", action="store_true")

    p_is = sub.add_parser("issue", help="Issue an offline activation token bound to machine code")
    p_is.add_argument("--machine-code", required=True, help="Buyer machine code e.g. SS-XXXX-XXXX-XXXX")
    p_is.add_argument("--tier", choices=list(VALID_PAID_TIERS), default="pro")
    p_is.add_argument("--expires", default=None, help="Optional YYYY-MM-DD expiry date (omit for perpetual)")
    p_is.add_argument("--note", default="", help="Order ID or customer label stored inside signed token")
    p_is.add_argument("--private-key", default=str(DEFAULT_PRIVATE_KEY_PATH))
    p_is.add_argument("--json", action="store_true")

    p_vf = sub.add_parser("verify", help="Verify an activation token offline")
    p_vf.add_argument("--token", required=True)
    p_vf.add_argument("--machine-code", default=None)
    p_vf.add_argument("--public-key-b64", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "keygen":
        return _cmd_keygen(args)
    if args.command == "issue":
        return _cmd_issue(args)
    if args.command == "verify":
        return _cmd_verify(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
