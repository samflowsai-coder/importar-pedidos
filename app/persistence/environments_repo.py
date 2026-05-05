"""CRUD da tabela `environments` em `app_shared.db`.

Senha do Firebird cifrada via `app/security/secret_store.py` (Fernet).
slug é imutável após `create()` — vira parte do nome do arquivo
`app_state_<slug>.db` e não pode mudar sem migração de dados.

Funções públicas:
- `create(...)`: insere; falha com `SlugTaken` se UNIQUE violado
- `get(env_id)` / `get_by_slug(slug)`: leitura pontual (public view, sem senha)
- `list_active()` / `list_all()`: listagens
- `update(env_id, ...)`: atualiza campos editáveis (slug é ignorado se passado)
- `get_password(env_id)`: retorna senha em claro (decrypt) ou None
- `soft_delete(env_id)`: marca `is_active=0` (preserva histórico de pedidos)
- `to_fb_config(env)`: materializa dict pronto para `app/erp/connection`
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from app.persistence import router
from app.security import secret_store

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_PUBLIC_FIELDS = (
    "id", "slug", "name", "watch_dir", "output_dir",
    "fb_path", "fb_host", "fb_port", "fb_user", "fb_charset",
    "is_active", "created_at", "updated_at",
)


class SlugTaken(Exception):
    """Slug já existe (violação de UNIQUE)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in _PUBLIC_FIELDS}


def create(
    *,
    slug: str,
    name: str,
    watch_dir: str,
    output_dir: str,
    fb_path: str,
    fb_host: str | None = None,
    fb_port: str | None = None,
    fb_user: str = "SYSDBA",
    fb_charset: str = "WIN1252",
    fb_password: str | None = None,
) -> dict[str, Any]:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ValueError(
            f"slug inválido: {slug!r} — use [a-z0-9-], 1-31 chars, começa com alfanum"
        )
    if not name or not name.strip():
        raise ValueError("name é obrigatório")
    env_id = str(uuid.uuid4())
    now = _now()
    pw_enc = secret_store.encrypt(fb_password) if fb_password else None

    try:
        with router.shared_connect() as conn:
            conn.execute(
                """INSERT INTO environments
                   (id, slug, name, watch_dir, output_dir, fb_path, fb_host, fb_port,
                    fb_user, fb_charset, fb_password_enc, is_active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (env_id, slug, name.strip(), watch_dir, output_dir, fb_path,
                 fb_host or None, fb_port or None, fb_user, fb_charset,
                 pw_enc, now, now),
            )
    except sqlite3.IntegrityError as exc:
        msg = str(exc).lower()
        if "unique" in msg and "slug" in msg:
            raise SlugTaken(slug) from exc
        raise
    return get(env_id)


def get(env_id: str) -> dict[str, Any] | None:
    with router.shared_connect() as conn:
        row = conn.execute(
            "SELECT * FROM environments WHERE id = ?", (env_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_by_slug(slug: str) -> dict[str, Any] | None:
    with router.shared_connect() as conn:
        row = conn.execute(
            "SELECT * FROM environments WHERE slug = ?", (slug,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_active() -> list[dict[str, Any]]:
    with router.shared_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM environments WHERE is_active = 1 ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_all() -> list[dict[str, Any]]:
    with router.shared_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM environments ORDER BY is_active DESC, name COLLATE NOCASE"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update(
    env_id: str,
    *,
    name: str | None = None,
    watch_dir: str | None = None,
    output_dir: str | None = None,
    fb_path: str | None = None,
    fb_host: str | None = None,
    fb_port: str | None = None,
    fb_user: str | None = None,
    fb_charset: str | None = None,
    fb_password: str | None = None,
) -> dict[str, Any] | None:
    """Atualiza campos editáveis. `slug` propositalmente ausente — imutável.

    Semântica de senha:
    - `fb_password=None`  → mantém valor atual (típico em edits parciais)
    - `fb_password=""`    → limpa (define NULL)
    - `fb_password="..."` → substitui (re-encrypt)
    """
    fields: dict[str, Any] = {}
    for k, v in {
        "name": name.strip() if isinstance(name, str) else name,
        "watch_dir": watch_dir,
        "output_dir": output_dir,
        "fb_path": fb_path,
        "fb_host": fb_host,
        "fb_port": fb_port,
        "fb_user": fb_user,
        "fb_charset": fb_charset,
    }.items():
        if v is not None:
            fields[k] = v
    if fb_password is not None:
        fields["fb_password_enc"] = secret_store.encrypt(fb_password) if fb_password else None
    if not fields:
        return get(env_id)
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [env_id]
    with router.shared_connect() as conn:
        conn.execute(f"UPDATE environments SET {sets} WHERE id = ?", values)
    return get(env_id)


def soft_delete(env_id: str) -> None:
    with router.shared_connect() as conn:
        conn.execute(
            "UPDATE environments SET is_active = 0, updated_at = ? WHERE id = ?",
            (_now(), env_id),
        )


def get_password(env_id: str) -> str | None:
    with router.shared_connect() as conn:
        row = conn.execute(
            "SELECT fb_password_enc FROM environments WHERE id = ?", (env_id,)
        ).fetchone()
    if not row or not row[0]:
        return None
    return secret_store.decrypt(row[0])


def to_fb_config(env: dict[str, Any]) -> dict[str, Any]:
    """Dict pronto para `app/erp/connection.connect_with_config(...)`."""
    return {
        "path": env["fb_path"],
        "host": env["fb_host"] or "",
        "port": env["fb_port"] or "",
        "user": env["fb_user"],
        "charset": env["fb_charset"],
        "password": get_password(env["id"]) or "",
    }
