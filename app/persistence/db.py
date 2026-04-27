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

-- Append-only lifecycle log. The state machine is the only writer.
-- (portal_status, production_status) on `imports` is a projection of this log.
CREATE TABLE IF NOT EXISTS order_lifecycle_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id    TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    source       TEXT NOT NULL,
    payload_json TEXT,
    trace_id     TEXT,
    occurred_at  TEXT NOT NULL,
    ingested_at  TEXT NOT NULL,
    FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE CASCADE
);

-- Auth (Fase 4b). Roles: 'admin' | 'operator' | 'viewer' (informational
-- today; Phase 4b only enforces "logged in"). bcrypt password_hash includes
-- its own salt + cost — store as-is.
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'operator',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);

-- One-shot invitation tokens. Admin issues one per email; invitee accepts
-- by setting a password. After accept, `accepted_at` is stamped and the
-- token cannot be used again.
--
-- Why a separate table from `users`: an invite can be revoked, can expire,
-- and several can be issued for the same email over time (only one open
-- at a time though — UNIQUE(email) WHERE accepted_at IS NULL would be
-- ideal but SQLite partial-unique-index syntax is limited; we enforce
-- "only one pending per email" in application code).
CREATE TABLE IF NOT EXISTS user_invites (
    token              TEXT PRIMARY KEY,
    email              TEXT NOT NULL COLLATE NOCASE,
    role               TEXT NOT NULL DEFAULT 'operator',
    invited_by_user_id INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    accepted_at        TEXT,
    accepted_user_id   INTEGER,
    revoked_at         TEXT,
    FOREIGN KEY (invited_by_user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (accepted_user_id)   REFERENCES users(id) ON DELETE SET NULL
);

-- Session cookie store. `token` is a high-entropy random string set as
-- HttpOnly cookie. `expires_at` is the absolute hard cap (TTL). On every
-- request we check it; expired sessions are deleted lazily.
CREATE TABLE IF NOT EXISTS sessions (
    token        TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    ip           TEXT,
    user_agent   TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Inbound webhook idempotency. Replayed webhooks (Gestor retrying, network
-- blip) hit the same (provider, event_id) and short-circuit with the
-- cached response. PRIMARY KEY enforces dedup at DB level.
CREATE TABLE IF NOT EXISTS inbound_idempotency (
    provider         TEXT NOT NULL,
    event_id         TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    response_status  INTEGER,
    response_body    TEXT,
    import_id        TEXT,            -- denormalized for debug; nullable
    PRIMARY KEY (provider, event_id)
);

-- Durable outbox for outbound integrations (Gestor de Produção, future
-- targets). The Portal writes to this table in the same SQLite transaction
-- as the state machine event, then a worker (Phase 5) — or inline drain
-- (Phase 3) — POSTs to the target with retries.
--
-- Idempotency: `idempotency_key` is UNIQUE; the target server uses it to
-- dedupe replays caused by retries / restarts.
CREATE TABLE IF NOT EXISTS outbox (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id        TEXT NOT NULL,
    target           TEXT NOT NULL,
    endpoint         TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TEXT,
    last_error       TEXT,
    response_json    TEXT,
    trace_id         TEXT,
    created_at       TEXT NOT NULL,
    sent_at          TEXT,
    FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE CASCADE
);

-- Token-bucket state for rate limiting (Fase 6).
-- `tokens` is a float in [0, capacity]; `last_refill_at` is a unix
-- timestamp (float seconds). Rows are written atomically inside a
-- SQLite DEFERRED transaction — no external lock needed at our scale.
-- Stale rows (inactive keys) are pruned by the retention job daily.
CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    key            TEXT PRIMARY KEY,
    tokens         REAL NOT NULL,
    last_refill_at REAL NOT NULL
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

CREATE INDEX IF NOT EXISTS idx_lifecycle_import_id   ON order_lifecycle_events(import_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_lifecycle_trace_id    ON order_lifecycle_events(trace_id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_event_type  ON order_lifecycle_events(event_type, occurred_at DESC);

-- Worker drain query in Phase 5 will be: WHERE status='pending' AND next_attempt_at <= NOW
-- ORDER BY next_attempt_at. This index covers it.
CREATE INDEX IF NOT EXISTS idx_outbox_pending       ON outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_outbox_import_id     ON outbox(import_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_inbound_received_at  ON inbound_idempotency(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbound_import_id    ON inbound_idempotency(import_id);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id     ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at  ON sessions(expires_at);

CREATE INDEX IF NOT EXISTS idx_invites_email_pending ON user_invites(email)
    WHERE accepted_at IS NULL AND revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_invites_expires_at    ON user_invites(expires_at);

-- Fase 5: worker poll — cobre a query list_pending_for_fire_poll
CREATE INDEX IF NOT EXISTS idx_imports_fire_poll
    ON imports(portal_status, production_status, fire_status_polled_at)
    WHERE fire_codigo IS NOT NULL;
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
    # State machine fundation (Fase 1)
    ("imports", "trace_id",        "ALTER TABLE imports ADD COLUMN trace_id TEXT"),
    ("imports", "state_version",   "ALTER TABLE imports ADD COLUMN state_version INTEGER NOT NULL DEFAULT 1"),
    # Outbox + Gestor de Produção integration (Fase 3)
    ("imports", "gestor_order_id", "ALTER TABLE imports ADD COLUMN gestor_order_id TEXT"),
    # Webhooks inbound + Apontaê correlation (Fase 4)
    ("imports", "apontae_order_id", "ALTER TABLE imports ADD COLUMN apontae_order_id TEXT"),
    # Manual cliente override after CLIENT_NOT_FOUND (sidecar — não muta snapshot).
    # `cliente_override_by` fica NULL hoje; preenchido quando auth (v5) chegar.
    ("imports", "cliente_override_codigo", "ALTER TABLE imports ADD COLUMN cliente_override_codigo INTEGER"),
    ("imports", "cliente_override_razao",  "ALTER TABLE imports ADD COLUMN cliente_override_razao TEXT"),
    ("imports", "cliente_override_at",     "ALTER TABLE imports ADD COLUMN cliente_override_at TEXT"),
    ("imports", "cliente_override_by",     "ALTER TABLE imports ADD COLUMN cliente_override_by TEXT"),
    # Poll worker (Fase 5): rastrear último status visto no Firebird e quando foi polled.
    ("imports", "fire_status_last_seen", "ALTER TABLE imports ADD COLUMN fire_status_last_seen TEXT"),
    ("imports", "fire_status_polled_at", "ALTER TABLE imports ADD COLUMN fire_status_polled_at TEXT"),
)


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    cols_by_table: dict[str, set[str]] = {}
    for table, col, ddl in _COLUMN_MIGRATIONS:
        if table not in cols_by_table:
            try:
                cols_by_table[table] = _existing_columns(conn, table)
            except sqlite3.OperationalError:
                # table not yet created — CREATE IF NOT EXISTS handles it
                cols_by_table[table] = set()
                continue
        if col not in cols_by_table[table]:
            conn.execute(ddl)
            cols_by_table[table].add(col)


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
