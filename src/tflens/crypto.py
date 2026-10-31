"""Symmetric encryption for secrets stored in SQLite.

The Fernet key is taken from ``TFLENS_SECRET_KEY`` if set, otherwise it is
generated once and stored next to the database with 0600 permissions.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    @classmethod
    def from_path(cls, key_path: Path) -> SecretBox:
        env_key = os.environ.get("TFLENS_SECRET_KEY")
        if env_key:
            return cls(env_key.encode())
        if key_path.exists():
            return cls(key_path.read_bytes().strip())
        key = Fernet.generate_key()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return cls(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("stored secret cannot be decrypted with the current key") from exc

    def __repr__(self) -> str:  # never expose key material
        return "SecretBox(<redacted>)"


class LazySecretBox:
    """A :class:`SecretBox` whose key file is only read / created on first use, so that
    starting the app writes nothing until a secret is actually stored."""

    def __init__(self, key_path: Path):
        self.key_path = key_path
        self._box: SecretBox | None = None
        self._lock = threading.Lock()

    def _get(self) -> SecretBox:
        with self._lock:
            if self._box is None:
                self._box = SecretBox.from_path(self.key_path)
            return self._box

    def encrypt(self, plaintext: str) -> str:
        return self._get().encrypt(plaintext)

    def decrypt(self, token: str) -> str:
        return self._get().decrypt(token)

    def __repr__(self) -> str:
        return "LazySecretBox(<redacted>)"
