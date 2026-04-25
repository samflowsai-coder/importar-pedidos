"""SQLite persistence for Portal de Pedidos state.

Schema is intentionally narrow: ONE import row per file processed. Items live
inside `snapshot_json` (the Order.model_dump() payload) — no normalized items
table. Diffing in Fase 3 walks the JSON against the live Firebird state.

Design:
- stdlib `sqlite3`, no ORM
- WAL mode: concurrent reads while writes are serialized by the DB
- foreign_keys ON
- per-request connection (short-lived, no thread pooling needed at our scale)
- all queries use ? placeholders — never string-interpolate input
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from app import config as app_config

_SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS imports (
    id               TEXT PRIMARY KEY,
    source_filename  TEXT NOT NULL,
    imported_at      TEXT NOT NULL,
    order_number     TEXT,
    customer_cnpj    TEXT,
    customer_name    TEXT,
    fire_codigo      INTEGER,
    snapshot_json    TEXT,
    check_json       TEXT,
    output_files_json TEXT,
    db_result_json   TEXT,
    status           TEXT NOT NULL,
    error            TEXT,
    portal_status    TEXT NOT NULL DEFAULT 'sent_to_fire',
    sent_to_fire_at  TEXT,
    production_status TEXT NOT NULL DEFAULT 'none',
    released_at      TEXT,
    released_by      TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    detail_json TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE CASCADE
);
"""

# Indexes applied AFTER migrations so new columns in older DBs already exist.
_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_imports_imported_at   ON imports(imported_at DESC);
CREATE INDEX IF NOT EXISTS idx_imports_customer_cnpj ON imports(customer_cnpj);
CREATE INDEX IF NOT EXISTS idx_imports_fire_codigo   ON imports(fire_codigo);
CREATE INDEX IF NOT EXISTS idx_imports_status        ON imports(status);
CREATE INDEX IF NOT EXISTS idx_imports_portal_status ON imports(portal_status);
CREATE INDEX IF NOT EXISTS idx_imports_prod_status   ON imports(production_status);

CREATE INDEX IF NOT EXISTS idx_audit_import_id ON audit_log(import_id);
CREATE INDEX IF NOT EXISTS idx_audit_created   ON audit_log(created_at DESC);
"""


_db_path_override: Optional[Path] = None
_initialized_paths: set[str] = set()
_init_lock = threading.Lock()


def db_path() -> Path:
    if _db_path_override is not None:
        return _db_path_override
    cfg = app_config.load()
    base = app_config.imported_dir(cfg)
    base.mkdir(parents=True, exist_ok=True)
    return base / "app_state.db"


def set_db_path(path: Optional[Path]) -> None:
    """Override the DB path. Used by tests and the migration tool."""
    global _db_path_override
    with _init_lock:
        _db_path_override = path


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")


_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # (table, column, ADD COLUMN ... DDL)
    ("imports", "check_json",      "ALTER TABLE imports ADD COLUMN check_json TEXT"),
    ("imports", "portal_status",   "ALTER TABLE imports ADD COLUMN portal_status TEXT NOT NULL DEFAULT 'sent_to_fire'"),
    ("imports", "sent_to_fire_at", "ALTER TABLE imports ADD COLUMN sent_to_fire_at TEXT"),
)


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    try:
        cols = _existing_columns(conn, "imports")
    except sqlite3.OperationalError:
        return  # table not yet created — CREATE IF NOT EXISTS handles it
    for table, col, ddl in _COLUMN_MIGRATIONS:
        if table == "imports" and col not in cols:
            conn.execute(ddl)


def _ensure_schema(path: Path) -> None:
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
            # Order matters: tables → migrations (add missing cols) → indexes
            # so indexes referencing migrated columns never fail on legacy DBs.
            conn.executescript(_SCHEMA_TABLES)
            _apply_migrations(conn)
            conn.executescript(_SCHEMA_INDEXES)
            conn.commit()
        finally:
            conn.close()
        _initialized_paths.add(key)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Short-lived connection with WAL + FK enforcement. Commits on success."""
    path = db_path()
    _ensure_schema(path)
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


def init() -> None:
    """Create schema explicitly. Idempotent — also triggered lazily by connect()."""
    _ensure_schema(db_path())


def reset_init_cache() -> None:
    """Force schema re-check on next connect(). Only for tests."""
    with _init_lock:
        _initialized_paths.clear()
