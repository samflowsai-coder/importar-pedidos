"""Schema do banco SQLite compartilhado (`app_shared.db`).

Hospeda metadata transversal: autenticação, sessões, ambientes (multi-empresa),
idempotência de webhooks inbound, rate-limit buckets. Nada que seja
financeiro/operacional de um pedido vive aqui — isso fica no schema_env.
"""
from __future__ import annotations

TABLES_SQL = """
-- Multi-empresa (MM, Nasmar, ...). slug é imutável após create e dele
-- deriva o nome do arquivo `app_state_<slug>.db`. Senha cifrada via
-- secret_store (Fernet). is_active=0 esconde da UI mas preserva FKs.
CREATE TABLE IF NOT EXISTS environments (
    id              TEXT PRIMARY KEY,
    slug            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    watch_dir       TEXT NOT NULL,
    output_dir      TEXT NOT NULL,
    fb_path         TEXT NOT NULL,
    fb_host         TEXT,
    fb_port         TEXT,
    fb_user         TEXT NOT NULL DEFAULT 'SYSDBA',
    fb_charset      TEXT NOT NULL DEFAULT 'WIN1252',
    fb_password_enc TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'operator',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);

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

CREATE TABLE IF NOT EXISTS sessions (
    token        TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    ip           TEXT,
    user_agent   TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS inbound_idempotency (
    provider         TEXT NOT NULL,
    event_id         TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    response_status  INTEGER,
    response_body    TEXT,
    import_id        TEXT,
    PRIMARY KEY (provider, event_id)
);

CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    key            TEXT PRIMARY KEY,
    tokens         REAL NOT NULL,
    last_refill_at REAL NOT NULL
);
"""

INDEXES_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_environments_slug ON environments(slug);
CREATE INDEX IF NOT EXISTS idx_environments_active      ON environments(is_active);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

CREATE INDEX IF NOT EXISTS idx_invites_email_pending ON user_invites(email)
    WHERE accepted_at IS NULL AND revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_invites_expires_at    ON user_invites(expires_at);

CREATE INDEX IF NOT EXISTS idx_inbound_received_at  ON inbound_idempotency(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbound_import_id    ON inbound_idempotency(import_id);
"""

# Migrações de coluna para shared.db (vazio por ora — schema novo).
COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = ()
