"""Encrypted credential vault.

Secrets (LLM provider keys, the Notion token, the No Frills session token) are encrypted with AES-256-GCM
under a master key that lives in the environment (MASTER_KEY), never in the database or its backups. The
secret's name is bound in as associated data, so a ciphertext can't be moved to another name.

The API is write-only by design: callers can set, delete and see status (set/unset, last 4 characters,
when last updated/used). Only server code reads plaintext, and only at the moment it's needed.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from shopping.models import Secret, utcnow

log = logging.getLogger(__name__)

# Names the application knows about; anything else is refused so the API can't become a general store.
KNOWN = {
    "notion": "Notion integration token",
    "llm:gemini": "Google Gemini API key",
    "llm:openai": "OpenAI API key",
    "llm:anthropic": "Anthropic API key",
    "llm:groq": "Groq API key",
    "llm:openrouter": "OpenRouter API key",
    "llm:mistral": "Mistral API key",
    "llm:custom": "API key for a custom OpenAI-compatible endpoint",
    "notify:ntfy": "ntfy topic URL for deal alerts (optional)",
}


class VaultError(Exception):
    pass


def load_master_key(b64: str, fallback_file: Path | None) -> bytes:
    """MASTER_KEY from the environment; for development only, a generated key file (with a warning)."""
    if b64:
        try:
            key = base64.b64decode(b64, validate=True)
        except ValueError as exc:
            raise VaultError("MASTER_KEY is not valid base64.") from exc
        if len(key) != 32:
            raise VaultError("MASTER_KEY must decode to exactly 32 bytes (openssl rand -base64 32).")
        return key
    if fallback_file is None:
        raise VaultError("MASTER_KEY is not set.")
    if fallback_file.is_file():
        key = fallback_file.read_bytes()
    else:
        fallback_file.parent.mkdir(parents=True, exist_ok=True)
        key = AESGCM.generate_key(bit_length=256)
        fd = os.open(fallback_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
    log.warning("MASTER_KEY not set: using the development key file %s. Set MASTER_KEY in production.", fallback_file)
    if len(key) != 32:
        raise VaultError(f"{fallback_file} does not hold a 32-byte key.")
    return key


def _last4(value: str) -> str | None:
    v = value.strip()
    return v[-4:] if len(v) >= 12 else None  # don't reveal part of something short


class Vault:
    def __init__(self, master_key: bytes):
        self._aead = AESGCM(master_key)

    @staticmethod
    def check_name(name: str) -> str:
        if name not in KNOWN:
            raise VaultError(f"Unknown secret {name!r}.")
        return name

    def set(self, session: Session, name: str, value: str, *, by: str | None = None) -> None:
        self.check_name(name)
        value = value.strip()
        if not value:
            raise VaultError("Empty value; use delete to remove a secret.")
        if len(value) > 8192:
            raise VaultError("Value is too long.")
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, value.encode(), name.encode())
        row = session.get(Secret, name) or Secret(name=name)
        row.nonce, row.ciphertext = nonce.hex(), base64.b64encode(ct).decode()
        row.last4, row.updated_at, row.updated_by = _last4(value), utcnow(), by
        session.add(row)
        session.flush()

    def get(self, session: Session, name: str, *, touch: bool = True) -> str | None:
        self.check_name(name)
        row = session.get(Secret, name)
        if row is None:
            return None
        try:
            value = self._aead.decrypt(bytes.fromhex(row.nonce), base64.b64decode(row.ciphertext), name.encode()).decode()
        except Exception as exc:  # noqa: BLE001 - wrong master key or tampering
            raise VaultError(f"Can't decrypt {name!r}: wrong MASTER_KEY or the stored value was altered.") from exc
        if touch:
            row.last_used_at = utcnow()
        return value

    def delete(self, session: Session, name: str) -> bool:
        self.check_name(name)
        row = session.get(Secret, name)
        if row is None:
            return False
        session.delete(row)
        session.flush()
        return True

    def status(self, session: Session) -> list[dict]:
        rows = {r.name: r for r in session.scalars(select(Secret))}
        out = []
        for name, label in KNOWN.items():
            r = rows.get(name)
            out.append({"name": name, "label": label, "set": r is not None, "last4": r.last4 if r else None,
                        "updated_at": r.updated_at.isoformat() if r else None, "updated_by": r.updated_by if r else None,
                        "last_used_at": r.last_used_at.isoformat() if r and r.last_used_at else None})
        return out
