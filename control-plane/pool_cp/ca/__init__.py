"""Built-in device CA and task-envelope signing key.

SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

Plan §8.6 names step-ca as the device CA. This module implements the same
contract (short-lived Ed25519 client certificates, deny list on revocation)
so Phase 1 runs without extra services; a step-ca backend can implement
`DeviceCA` later (deviation X1 in docs/design/phase1-implementation.md).
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ..timeutil import utcnow


class CAError(Exception):
    pass


@dataclass(frozen=True)
class VerifiedCert:
    device_id: str
    serial: str
    public_key: Ed25519PublicKey
    public_key_raw: bytes
    not_after: datetime


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _load_or_create_key(path: Path, create: bool) -> Ed25519PrivateKey:
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise CAError(f"{path} is not an Ed25519 private key")
        return key
    if not create:
        raise CAError(f"missing key file {path} (set POOL_AUTOCREATE_KEYS=true to create it)")
    key = Ed25519PrivateKey.generate()
    _write_private(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return key


def raw_public(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class LocalCA:
    def __init__(self, key_file: Path, cert_file: Path, cert_ttl: timedelta, create: bool) -> None:
        self.key = _load_or_create_key(key_file, create)
        if cert_file.exists():
            self.cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
        elif create:
            self.cert = self._self_signed()
            cert_file.parent.mkdir(parents=True, exist_ok=True)
            cert_file.write_bytes(self.cert.public_bytes(serialization.Encoding.PEM))
        else:
            raise CAError(f"missing CA certificate {cert_file}")
        if raw_public(self.cert.public_key()) != raw_public(self.key.public_key()):  # type: ignore[arg-type]
            raise CAError("CA certificate does not match CA key")
        self.cert_ttl = cert_ttl

    @property
    def cert_pem(self) -> str:
        return self.cert.public_bytes(serialization.Encoding.PEM).decode()

    def _self_signed(self) -> x509.Certificate:
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pool device CA")])
        now = utcnow()
        return (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .sign(self.key, algorithm=None)
        )

    @staticmethod
    def csr_public_key(csr_pem: str) -> bytes:
        """Validate a device CSR and return its raw Ed25519 public key."""
        try:
            csr = x509.load_pem_x509_csr(csr_pem.encode())
        except ValueError as exc:
            raise CAError("invalid CSR") from exc
        if not csr.is_signature_valid:
            raise CAError("CSR signature is invalid")
        pub = csr.public_key()
        if not isinstance(pub, Ed25519PublicKey):
            raise CAError("device keys must be Ed25519")
        return raw_public(pub)

    def issue(self, public_key_raw: bytes, device_id: str) -> tuple[str, str, datetime]:
        """Issue a short-lived client certificate. Returns (pem, serial_hex, not_after)."""
        now = utcnow()
        not_after = now + self.cert_ttl
        serial = x509.random_serial_number()
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)]))
            .issuer_name(self.cert.subject)
            .public_key(Ed25519PublicKey.from_public_bytes(public_key_raw))
            .serial_number(serial)
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .sign(self.key, algorithm=None)
        )
        return cert.public_bytes(serialization.Encoding.PEM).decode(), format(serial, "x"), not_after

    def verify(self, cert_pem: str) -> VerifiedCert:
        """Check issuer signature and validity period. Revocation is checked by the caller (DB)."""
        try:
            cert = x509.load_pem_x509_certificate(cert_pem.encode())
            cert.verify_directly_issued_by(self.cert)
        except (ValueError, TypeError, InvalidSignature) as exc:
            raise CAError("certificate not issued by this CA") from exc
        now = utcnow()
        if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
            raise CAError("certificate expired or not yet valid")
        pub = cert.public_key()
        if not isinstance(pub, Ed25519PublicKey):
            raise CAError("certificate key is not Ed25519")
        cns = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if len(cns) != 1:
            raise CAError("certificate must have exactly one CN")
        return VerifiedCert(device_id=str(cns[0].value), serial=format(cert.serial_number, "x"), public_key=pub,
                            public_key_raw=raw_public(pub), not_after=cert.not_valid_after_utc)


class EnvelopeSigner:
    """Signs task envelopes (plan §7.12). Agents pin the public key at enrollment."""

    def __init__(self, key_file: Path, create: bool) -> None:
        self.key = _load_or_create_key(key_file, create)
        pub = raw_public(self.key.public_key())
        self.key_id = "k-" + hashlib.sha256(pub).hexdigest()[:16]
        self.public_key_b64 = base64.b64encode(pub).decode()

    def sign(self, payload: bytes) -> str:
        return base64.b64encode(self.key.sign(payload)).decode()


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
