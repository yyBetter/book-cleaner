"""Pure-Python self-signed LAN HTTPS certificate generator (no OpenSSL CLI required)."""

from __future__ import annotations

import ipaddress
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def ensure_lan_self_signed_cert(cert_dir: Path | str, lan_ip: str) -> tuple[Path, Path]:
    """Ensure a valid X.509 v3 self-signed cert+key exists for `lan_ip`, 127.0.0.1, and localhost."""
    target_dir = Path(cert_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    cert_path = target_dir / "cert.pem"
    key_path = target_dir / "key.pem"
    meta_path = target_dir / "cert_meta.json"

    if cert_path.exists() and key_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            existing = x509.load_pem_x509_certificate(cert_path.read_bytes())
            if (
                meta.get("lan_ip") == lan_ip
                and existing.not_valid_after_utc > datetime.now(timezone.utc) + timedelta(days=7)
            ):
                return cert_path, key_path
        except Exception:
            pass

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Book Cleaner Local LAN"),
            x509.NameAttribute(NameOID.COMMON_NAME, lan_ip or "localhost"),
        ]
    )

    san_entries: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    if lan_ip and lan_ip != "127.0.0.1":
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(lan_ip)))
        except ValueError:
            san_entries.append(x509.DNSName(lan_ip))

    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )

    key_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_bytes = cert.public_bytes(serialization.Encoding.PEM)

    key_path.write_bytes(key_bytes)
    cert_path.write_bytes(cert_bytes)
    meta_path.write_text(
        json.dumps({"lan_ip": lan_ip, "created_at": now.isoformat()}, indent=2),
        encoding="utf-8",
    )
    return cert_path, key_path
