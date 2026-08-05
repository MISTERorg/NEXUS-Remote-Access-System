"""
utils/crypto.py
---------------
Cryptographic operations for NEXUS:
- Password hashing (bcrypt / SHA-256 fallback)
- AES-256-GCM authenticated encryption
- ECDH ephemeral key exchange (SECP256R1)
- TLS Certificate generation
"""

from __future__ import annotations

import datetime
import os
import secrets
from pathlib import Path
from typing import Tuple, Union

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.oid import NameOID

try:
    import bcrypt
    _HAS_BCRYPT = True
except ImportError:
    _HAS_BCRYPT = False
    import hashlib


# ---------------------------------------------------------------------------
# Password Hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hashes a password using bcrypt if available, otherwise SHA-256 with salt."""
    if _HAS_BCRYPT:
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")
    else:
        salt = os.urandom(16).hex()
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
        ).hex()
        return f"pbkdf2_sha256${salt}${digest}"


def verify_password(password: str, hashed: str) -> bool:
    """Verifies a plain password against its hashed representation."""
    if _HAS_BCRYPT and not hashed.startswith("pbkdf2_sha256$"):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
        except Exception:
            return False
    elif hashed.startswith("pbkdf2_sha256$"):
        try:
            _, salt, digest = hashed.split("$")
            check = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
            ).hex()
            return secrets.compare_digest(check, digest)
        except Exception:
            return False
    return False


# ---------------------------------------------------------------------------
# Symmetric Encryption (AES-256-GCM)
# ---------------------------------------------------------------------------

class AESGCMCipher:
    """AES-256-GCM AEAD cipher wrapper."""

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("AES-256-GCM key must be exactly 32 bytes")
        self._aesgcm = AESGCM(key)

    def encrypt(self, plaintext: bytes, aad: bytes = b"") -> bytes:
        nonce = os.urandom(12)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext, aad)
        return nonce + ciphertext

    def decrypt(self, ciphertext: bytes, aad: bytes = b"") -> bytes:
        if len(ciphertext) < 12:
            raise ValueError("Ciphertext too short")
        nonce = ciphertext[:12]
        data = ciphertext[12:]
        return self._aesgcm.decrypt(nonce, data, aad)


# ---------------------------------------------------------------------------
# Ephemeral Key Exchange (ECDH)
# ---------------------------------------------------------------------------

class ECDHKeyExchange:
    """Elliptic-Curve Diffie-Hellman key exchange over SECP256R1."""

    def __init__(self):
        self._private_key = ec.generate_private_key(ec.SECP256R1())

    def public_key_bytes(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def derive_shared_key(self, peer_public_bytes: bytes, info: bytes = b"nexus-session") -> bytes:
        peer_public_key = serialization.load_der_public_key(peer_public_bytes)
        if not isinstance(peer_public_key, ec.EllipticCurvePublicKey):
            raise ValueError("Invalid peer public key type")

        raw_shared = self._private_key.exchange(ec.ECDH(), peer_public_key)
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=info,
        )
        return hkdf.derive(raw_shared)


# ---------------------------------------------------------------------------
# TLS Certificate Generation
# ---------------------------------------------------------------------------

def generate_self_signed_cert(
    cert_path: str = "certs/relay.crt",
    key_path: str = "certs/relay.key",
    hostname: str = "localhost",
    valid_days: int = 365,
    out_dir: Union[str, Path, None] = None,
    public_ip: Union[str, None] = None,
    **kwargs,
) -> Tuple[str, str]:
    """
    Generates a self-signed TLS certificate and private key.

    Args:
        cert_path:  Output path for the certificate PEM (ignored when out_dir is set).
        key_path:   Output path for the private-key PEM  (ignored when out_dir is set).
        hostname:   CN and first DNS SAN (e.g. "nexus.local" or the relay's FQDN).
        valid_days: Certificate lifetime in days.
        out_dir:    If provided, cert/key are written as relay.crt / relay.key here.
        public_ip:  Public IPv4 of the relay machine.  When supplied it is added as an
                    IP SAN so that clients connecting by IP pass TLS verification.
                    Example: "122.183.33.42"

    Returns:
        (cert_path, key_path) as strings.
    """
    import ipaddress as _ipaddress

    if out_dir:
        target_dir = Path(out_dir)
        cert_path = str(target_dir / "relay.crt")
        key_path = str(target_dir / "relay.key")

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NEXUS"),
    ])

    # -----------------------------------------------------------------------
    # Subject Alternative Names
    # -----------------------------------------------------------------------
    # RFC 5280 §4.2.1.6 requires IP addresses to use IPAddress entries, NOT
    # DNSName entries.  Using DNSName("127.0.0.1") is technically invalid and
    # rejected by strict TLS implementations (OpenSSL ≥ 3, Python ssl module).
    # -----------------------------------------------------------------------
    # Fixed DNS SANs always present.  The hostname is appended below only if
    # it is not already in this set, so callers passing hostname="localhost"
    # or hostname="nexus.local" don't end up with duplicate entries.
    _fixed_dns = {"localhost", "nexus.local"}
    san_entries: list = [x509.DNSName(n) for n in sorted(_fixed_dns)]

    # Add the CN as a SAN only when it is not already covered above.
    try:
        _ipaddress.ip_address(hostname)
        # hostname is an IP literal — add it as an IPAddress SAN
        san_entries.append(x509.IPAddress(_ipaddress.ip_address(hostname)))
    except ValueError:
        # hostname is a DNS name — add only if not a duplicate
        if hostname not in _fixed_dns:
            san_entries.append(x509.DNSName(hostname))

    # Always include loopback as a proper IP SAN
    san_entries.append(x509.IPAddress(_ipaddress.ip_address("127.0.0.1")))

    # Include the relay's public IP so remote agents can verify the cert
    if public_ip and public_ip not in ("127.0.0.1", "localhost"):
        try:
            san_entries.append(x509.IPAddress(_ipaddress.ip_address(public_ip)))
        except ValueError:
            pass  # not a valid IP — silently skip

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=valid_days)
        )
        .add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    # Save Private Key
    Path(key_path).parent.mkdir(parents=True, exist_ok=True)
    with open(key_path, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))

    # Save Certificate
    Path(cert_path).parent.mkdir(parents=True, exist_ok=True)
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

