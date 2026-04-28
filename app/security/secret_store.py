"""Local Fernet-encrypted secret storage for UI-editable config.

This is *separate* from `app.security.secrets` — that module is a read-only
abstraction over backend (env today, Vault later) for runtime secrets like
API tokens. This module is for secrets the admin can edit via the web UI
(today: Firebird password) and need to round-trip on disk.

Key file (`app/.secret.key`) is generated lazily on first write, with
chmod 600, and is gitignored. If the key is lost, previously stored
ciphertexts cannot be recovered — `decrypt()` returns None and the admin
re-saves via UI. We never raise on decrypt failure to keep the app bootable.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.utils.logger import logger

_KEY_FILE = Path(__file__).parent.parent / ".secret.key"


def _key_path() -> Path:
    return _KEY_FILE


def _load_or_create_key() -> bytes:
    path = _key_path()
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Non-POSIX filesystems (e.g. some Windows setups) may reject chmod;
        # log and continue. The key is still in a gitignored file.
        logger.warning(f"chmod 600 falhou em {path} — verifique permissões manualmente")
    return key


def _fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string. Returns urlsafe base64 token."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str | None:
    """Decrypt a token previously produced by `encrypt`.

    Returns None on any failure (bad token, missing/rotated key) — never raises.
    Caller should treat None as "value unavailable; admin must re-save".
    """
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error(
            "secret_store.decrypt: token inválido — chave perdida ou rotacionada. "
            "Admin precisa re-salvar a config via UI."
        )
        return None
    except Exception as exc:  # noqa: BLE001 — defensive: never bubble up
        logger.error(f"secret_store.decrypt: falha inesperada ({exc!r})")
        return None


def key_exists() -> bool:
    """For diagnostics / tests."""
    return _key_path().exists()
