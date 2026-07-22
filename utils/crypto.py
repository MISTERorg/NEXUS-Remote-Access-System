"""
utils/crypto.py
---------------
Key management, certificate generation, AES-GCM encryption helpers,
ECDH ephemeral session key exchange.

Run standalone to generate self-signed CA + server/agent certs:
    python -m utils.crypto --generate-certs --out ./certs
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import os
import secrets
from pathlib import Path
from typing import Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.oid import NameOID


# ---------------------------------------------------------------------------
# AES-256-GCM helpers
# ---------------------------------------------------------------------------

class AESGCMCipher:
    """Authenticated encryption with AES-256-GCM."""

    KEY_BITS = 256
    NONCE_BYTES = 12

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("AES-256 requires a 32-byte key")
        self._aesgcm = AESGCM(key)

    @classmethod
    def generate_key(cls) -> bytes:
        return AESGCM.generate_key(bit_length=cls.KEY_BITS)

    def encrypt(self, plaintext: bytes, aad: bytes | None = None) -> bytes:
        """Returns nonce + ciphertext (nonce prepended for convenience)."""
        nonce = os.urandom(self.NONCE_BYTES)
        ct = self._aesgcm.encrypt(nonce, plaintext, aad)
        return nonce + ct

    def decrypt(self, data: bytes, aad: bytes | None = None) -> bytes:
        """Expects nonce + ciphertext as produced by encrypt()."""
        nonce, ct = data[: self.NONCE_BYTES], data[self.NONCE_BYTES :]
        return self._aesgcm.decrypt(nonce, ct, aad)


# ---------------------------------------------------------------------------
# ECDH ephemeral key exchange
# ---------------------------------------------------------------------------

class ECDHKeyExchange:
    """
    Ephemeral ECDH (P-256) key exchange.
    """

    CURVE = ec.SECP256R1()

    def __init__(self):
        self._private_key = ec.generate_private_key(self.CURVE)

    def public_key_bytes(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )

    def derive_shared_key(self, peer_public_bytes: bytes, info: bytes = b"nexus-session") -> bytes:
        peer_key = ec.EllipticCurvePublicKey.from_encoded_point(
            self.CURVE, peer_public_bytes
        )
        shared = self._private_key.exchange(ec.ECDH(), peer_key)
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=info,
        ).derive(shared)


# ---------------------------------------------------------------------------
# Certificate utilities
# ---------------------------------------------------------------------------

def _name(cn: str, org: str = "NEXUS") -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
    ])


def generate_ca_cert(
    out_dir: Path,
    cn: str = "NEXUS-CA",
    valid_days: int = 3650,
) -> Tuple[Path, Path]:
    """Generate a self-signed CA certificate and key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(_name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=valid_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path = out_dir / "ca.key"
    crt_path = out_dir / "ca.crt"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, crt_path


def generate_signed_cert(
    out_dir: Path,
    ca_key_path: Path,
    ca_crt_path: Path,
    cn: str,
    san_dns: list[str] | None = None,
    valid_days: int = 365,
) -> Tuple[Path, Path]:
    """Generate a certificate signed by the given CA."""
    ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
    ca_cert = x509.load_pem_x509_certificate(ca_crt_path.read_bytes())

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)

    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=valid_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )

    if san_dns:
        san = x509.SubjectAlternativeName([x509.DNSName(d) for d in san_dns])
        builder = builder.add_extension(san, critical=False)

    cert = builder.sign(ca_key, hashes.SHA256())

    prefix = cn.lower().replace(" ", "_")
    key_path = out_dir / f"{prefix}.key"
    crt_path = out_dir / f"{prefix}.crt"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, crt_path


def generate_device_token() -> str:
    """Generate a secure random device registration token."""
    return secrets.token_urlsafe(32)


def hash_password(password: str) -> str:
    """bcrypt-style password hash with fallback."""
    try:
        import bcrypt
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    except ImportError:
        salt = secrets.token_hex(16)
        h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
        return f"sha256${salt}${h}"


def verify_password(password: str, hashed: str) -> bool:
    if hashed.startswith("sha256$"):
        try:
            _, salt, h = hashed.split("$")
            return hashlib.sha256(f"{salt}{password}".encode()).hexdigest() == h
        except ValueError:
            return False
    try:
        import bcrypt
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ImportError:
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NEXUS crypto utilities")
    parser.add_argument("--generate-certs", action="store_true")
    parser.add_argument("--out", type=Path, default=Path("./certs"))
    args = parser.parse_args()

    if args.generate-certs:
        args.out.mkdir(parents=True, exist_ok=True)
        print("[+] Generating CA certificate...")
        ca_key, ca_crt = generate_ca_cert(args.out)
        print(f"    CA key : {ca_key}")
        print(f"    CA cert: {ca_crt}")

        print("[+] Generating server certificate...")
        s_key, s_crt = generate_signed_cert(
            args.out, ca_key, ca_crt,
            cn="nexus-relay-server",
            san_dns=["localhost", "nexus.local"],
        )
        print(f"    Key : {s_key}")
        print(f"    Cert: {s_crt}")

        print("[+] Generating agent certificate...")
        a_key, a_crt = generate_signed_cert(
            args.out, ca_key, ca_crt, cn="nexus-agent"
        )
        print(f"    Key : {a_key}")
        print(f"    Cert: {a_crt}")
        print("[✓] Done.")