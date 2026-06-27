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
from datetime import UTC, datetime
from typing import Any

from app.persistence import router
from app.security import secret_store

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_PUBLIC_FIELDS = (
    "id", "slug", "name", "watch_dir", "output_dir",
    "fb_path", "fb_host", "fb_port", "fb_user", "fb_charset",
    "is_active", "created_at", "updated_at",
    # FlowPCP (não-secreto). O token cifrado fica fora do public view.
    "flowpcp_enabled", "flowpcp_base_url", "flowpcp_tenant_id",
    "flowpcp_timezone", "flowpcp_dry_run", "flowpcp_poll_interval_s",
    "flowpcp_request_timeout_s",
)


class SlugTaken(Exception):
    """Slug já existe (violação de UNIQUE)."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean_path(value: str | None) -> str | None:
    """Strip surrounding whitespace + paired single/double quotes from a path.

    Macs (Finder "Copy as Pathname") and Windows (cmd path-with-spaces) often
    yield paths wrapped in quotes when pasted. Saving that raw breaks Firebird
    `connect()` with a confusing "io error: file not found"."""
    if value is None:
        return None
    s = value.strip()
    while len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        s = s[1:-1].strip()
    return s


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
    # Normaliza slug para lowercase antes de validar — UX permissiva.
    if isinstance(slug, str):
        slug = slug.strip().lower()
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ValueError(
            f"slug inválido: {slug!r} — use [a-z0-9-], 1-31 chars, começa com alfanum"
        )
    if not name or not name.strip():
        raise ValueError("name é obrigatório")
    env_id = str(uuid.uuid4())
    now = _now()
    pw_enc = secret_store.encrypt(fb_password) if fb_password else None
    fb_path_clean = _clean_path(fb_path) or ""

    try:
        with router.shared_connect() as conn:
            conn.execute(
                """INSERT INTO environments
                   (id, slug, name, watch_dir, output_dir, fb_path, fb_host, fb_port,
                    fb_user, fb_charset, fb_password_enc, is_active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (env_id, slug, name.strip(), watch_dir, output_dir, fb_path_clean,
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
        "fb_path": _clean_path(fb_path),
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


def set_flowpcp_config(
    env_id: str,
    *,
    enabled: bool,
    base_url: str | None,
    tenant_id: str | None,
    timezone: str = "America/Sao_Paulo",
    dry_run: bool = False,
    poll_interval_s: int = 30,
    request_timeout_s: float = 30.0,
    service_token: str | None = None,
) -> dict[str, Any] | None:
    """Grava a config FlowPCP do ambiente. Token cifrado via secret_store.

    Semântica de `service_token` (igual à senha do Firebird):
    - `None`  → mantém o token atual (edits que não mexem no segredo)
    - `""`    → limpa (NULL)
    - `"..."` → substitui (re-encrypt)

    Desligar (`enabled=False`) NÃO apaga o token — re-ligar não exige redigitar.
    """
    fields: dict[str, Any] = {
        "flowpcp_enabled": 1 if enabled else 0,
        "flowpcp_base_url": base_url or None,
        "flowpcp_tenant_id": tenant_id or None,
        "flowpcp_timezone": timezone or "America/Sao_Paulo",
        "flowpcp_dry_run": 1 if dry_run else 0,
        "flowpcp_poll_interval_s": int(poll_interval_s),
        "flowpcp_request_timeout_s": float(request_timeout_s),
        "updated_at": _now(),
    }
    if service_token is not None:
        fields["flowpcp_service_token_enc"] = (
            secret_store.encrypt(service_token) if service_token else None
        )
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [env_id]
    with router.shared_connect() as conn:
        conn.execute(f"UPDATE environments SET {sets} WHERE id = ?", values)
    return get(env_id)


def get_flowpcp_token(env_id: str) -> str | None:
    with router.shared_connect() as conn:
        row = conn.execute(
            "SELECT flowpcp_service_token_enc FROM environments WHERE id = ?", (env_id,)
        ).fetchone()
    if not row or not row[0]:
        return None
    return secret_store.decrypt(row[0])


def to_fb_config(env: dict[str, Any]) -> dict[str, Any]:
    """Dict pronto para `app/erp/connection.connect_with_config(...)`.

    `_clean_path` normaliza dados legados que tenham aspas em volta do path
    (pre-fix do bug do Copy Pathname). Saves novos já chegam limpos."""
    return {
        "path": _clean_path(env["fb_path"]) or "",
        "host": env["fb_host"] or "",
        "port": env["fb_port"] or "",
        "user": env["fb_user"],
        "charset": env["fb_charset"],
        "password": get_password(env["id"]) or "",
    }
