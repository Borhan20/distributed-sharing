"""Encryption at rest for secrets the server must keep, e.g. a group's registry pull token.

SECURITY-SENSITIVE: needs security review (plan §8.10).

Fernet (AES-128-CBC + HMAC-SHA256, from `cryptography`) with a key file next to the device CA
(Settings.secrets_key_file, default <data_dir>/secrets.key, mode 0600). A database dump alone does not
reveal the secrets; the key file must be backed up with the database, or the secrets are lost.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretBoxError(RuntimeError):
    pass


class SecretBox:
    def __init__(self, key_file: Path, create: bool) -> None:
        self.key_file = Path(key_file)
        self._fernet: Fernet | None = None
        self._create = create

    def _box(self) -> Fernet:
        if self._fernet is None:
            if not self.key_file.exists():
                if not self._create:
                    raise SecretBoxError(f"secrets key {self.key_file} is missing")
                self.key_file.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
            self._fernet = Fernet(self.key_file.read_bytes().strip())
        return self._fernet

    def seal(self, plaintext: str) -> bytes:
        return self._box().encrypt(plaintext.encode())

    def open(self, sealed: bytes) -> str:
        try:
            return self._box().decrypt(bytes(sealed)).decode()
        except InvalidToken as exc:
            raise SecretBoxError("cannot decrypt: wrong secrets key") from exc
