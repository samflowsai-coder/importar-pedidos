"""Roteamento de conexões SQLite multi-ambiente.

Uma DB compartilhada (`app_shared.db`) para auth/env metadata, e uma DB por
ambiente (`app_state_<slug>.db`) para dados operacionais. Slugs são validados
contra `SLUG_RE` antes de virarem nome de arquivo — defesa contra path
traversal e caracteres exóticos.

API:
- `shared_db_path()` / `env_db_path(slug)`: resolvem caminho
- `shared_connect()` / `env_connect(slug)`: context-managers que garantem
   schema aplicado e devolvem `sqlite3.Connection` com WAL + FK on
- `list_env_slugs()`: slugs ativos lidos da tabela `environments`

`reset_init_cache()` é só para testes — força re-aplicação do schema.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.persistence import schema_env, schema_shared

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")

_init_lock = threading.Lock()
_initialized_paths: set[str] = set()


def _data_dir() -> Path:
    raw = os.environ.get("APP_DATA_DIR", "").strip()
    if raw:
        base = Path(raw).expanduser().resolve()
    else:
        base = Path(__file__).resolve().parents[2] / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def shared_db_path() -> Path:
    return _data_dir() / "app_shared.db"


def env_db_path(slug: str) -> Path:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ValueError(f"slug inválido: {slug!r}")
    return _data_dir() / f"app_state_{slug}.db"


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _apply_column_migrations(
    conn: sqlite3.Connection,
    migrations: tuple[tuple[str, str, str], ...],
) -> None:
    cols_by_table: dict[str, set[str]] = {}
    for table, col, ddl in migrations:
        if table not in cols_by_table:
            try:
                cols_by_table[table] = _existing_columns(conn, table)
            except sqlite3.OperationalError:
                cols_by_table[table] = set()
                continue
        if col not in cols_by_table[table]:
            conn.execute(ddl)
            cols_by_table[table].add(col)


def _ensure_schema(path: Path, schema_module) -> None:
    key = str(path)
    if key in _initialized_paths:
        return
    with _init_lock:
        if key in _initialized_paths:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5.0)
        try:
            _configure(conn)
            conn.executescript(schema_module.TABLES_SQL)
            _apply_column_migrations(conn, schema_module.COLUMN_MIGRATIONS)
            conn.executescript(schema_module.INDEXES_SQL)
            conn.commit()
        finally:
            conn.close()
        _initialized_paths.add(key)


@contextmanager
def shared_connect() -> Iterator[sqlite3.Connection]:
    path = shared_db_path()
    _ensure_schema(path, schema_shared)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level="DEFERRED")
    _configure(conn)
    try:
        yield conn
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def env_connect(slug: str) -> Iterator[sqlite3.Connection]:
    path = env_db_path(slug)
    _ensure_schema(path, schema_env)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level="DEFERRED")
    _configure(conn)
    try:
        yield conn
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_env_slugs() -> list[str]:
    """Slugs de ambientes ativos. Usado por workers para iterar."""
    with shared_connect() as conn:
        rows = conn.execute(
            "SELECT slug FROM environments WHERE is_active = 1 ORDER BY slug"
        ).fetchall()
    return [r[0] for r in rows]


def reset_init_cache() -> None:
    """Reset cache de inicialização de schema. Apenas para testes."""
    with _init_lock:
        _initialized_paths.clear()
