"""UI-editable Firebird connection config.

Source of truth precedence (highest first):
1. `app/firebird.json` (managed by the UI; password encrypted via secret_store)
2. `FB_*` environment variables (read by `app/erp/connection.py:_get_env`)
3. Defaults baked into `connection.py`

`apply_to_env()` is called on FastAPI startup and after every successful
`POST /api/firebird/config`. It overwrites `os.environ` for the FB_* keys
that have a non-empty value in `firebird.json`. Because `_get_env` reads
`os.environ` on every connect (no cache), changes take effect immediately
with no restart.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.security import secret_store
from app.utils.logger import logger

_CONFIG_FILE = Path(__file__).parent / "firebird.json"

_PUBLIC_KEYS = ("path", "host", "port", "user", "charset")
_ENV_MAP = {
    "path": "FB_DATABASE",
    "host": "FB_HOST",
    "port": "FB_PORT",
    "user": "FB_USER",
    "charset": "FB_CHARSET",
}


def _empty_payload() -> dict[str, Any]:
    return {
        "path": "",
        "host": "",
        "port": "",
        "user": "",
        "charset": "",
        "password_enc": "",
    }


def load() -> dict[str, Any]:
    """Return the saved config (without decrypting the password).

    Always returns a dict with all expected keys (empty strings for missing).
    Never raises — falls back to empty payload on parse error.
    """
    if not _CONFIG_FILE.exists():
        return _empty_payload()
    try:
        raw = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(f"firebird_config.load: falha ao ler {_CONFIG_FILE} ({exc!r})")
        return _empty_payload()
    out = _empty_payload()
    for k in _PUBLIC_KEYS:
        v = raw.get(k)
        if v is not None:
            out[k] = str(v)
    if isinstance(raw.get("password_enc"), str):
        out["password_enc"] = raw["password_enc"]
    return out


def public_view() -> dict[str, Any]:
    """Return everything *except* the encrypted password (for GET endpoint)."""
    cfg = load()
    return {k: cfg[k] for k in _PUBLIC_KEYS}


def get_password() -> str | None:
    """Decrypt and return the saved password, or None if absent/unrecoverable."""
    cfg = load()
    if not cfg["password_enc"]:
        return None
    return secret_store.decrypt(cfg["password_enc"])


def save(payload: dict[str, Any], password: str | None) -> dict[str, Any]:
    """Persist config to disk.

    `password=None` means: keep the existing encrypted password (typical when
    admin edits other fields without re-typing the password). To clear the
    password, pass an empty string.
    """
    current = load()
    out: dict[str, Any] = {}
    for k in _PUBLIC_KEYS:
        v = payload.get(k)
        out[k] = "" if v is None else str(v).strip()
    if password is None:
        out["password_enc"] = current["password_enc"]
    elif password == "":
        out["password_enc"] = ""
    else:
        out["password_enc"] = secret_store.encrypt(password)
    _CONFIG_FILE.write_text(
        json.dumps(out, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return out


def is_configured() -> bool:
    return bool(load()["path"])


def apply_to_env() -> None:
    """Inject saved values into os.environ so connection.py picks them up.

    Only sets keys with a non-empty value — empty strings are not exported,
    so a user-supplied .env still wins for fields the UI hasn't filled.
    Password (if recoverable) is exported as FB_PASSWORD.
    """
    cfg = load()
    for key, env_name in _ENV_MAP.items():
        value = cfg.get(key, "")
        if value:
            os.environ[env_name] = value
    enc = cfg.get("password_enc")
    if enc:
        plain = secret_store.decrypt(enc)
        if plain is not None:
            os.environ["FB_PASSWORD"] = plain
