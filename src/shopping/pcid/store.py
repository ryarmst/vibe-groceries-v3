"""Encrypted JSON state on disk (AES-GCM under VG3_MASTER_KEY), with a cross-process lock.

PC ID refresh tokens are single-use and rotate, so two processes refreshing from the same
token would break the chain. Every read-refresh-write happens under an exclusive flock.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = b"vg3:1:"


class StateError(Exception):
    pass


class SecretStore:
    def __init__(self, path: Path, key: bytes):
        self.path = Path(path)
        self._aead = AESGCM(key)
        self._aad = self.path.name.encode()  # binds the ciphertext to its file name

    def read(self) -> dict:
        try:
            blob = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        if not blob.startswith(_VERSION):
            raise StateError(f"{self.path.name}: unknown format.")
        body = blob[len(_VERSION):]
        try:
            plain = self._aead.decrypt(body[:12], body[12:], self._aad)
        except Exception as exc:
            raise StateError(f"{self.path.name}: can't decrypt (wrong VG3_MASTER_KEY?).") from exc
        return json.loads(plain)

    def write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        nonce = os.urandom(12)
        blob = _VERSION + nonce + self._aead.encrypt(nonce, json.dumps(data).encode(), self._aad)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    @contextlib.contextmanager
    def locked(self):
        """Exclusive lock shared by every process using this state file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
