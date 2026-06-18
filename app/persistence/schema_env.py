"""Schema do banco SQLite por-ambiente (`app_state_<slug>.db`).

Uma DB por ambiente (MM, Nasmar, ...). Hospeda dados financeiro-operacionais
do pedido: importações, audit, lifecycle, outbox. Coluna `environment_id`
em todas as tabelas é defensiva — a DB já é específica de um ambiente, mas
o ID redundante torna trivial detectar bug de wiring.
"""
from __future__ import annotations

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS imports (
    id                       TEXT PRIMARY KEY,
    environment_id           TEXT NOT NULL,
    source_filename          TEXT NOT NULL,
    imported_at              TEXT NOT NULL,
    order_number             TEXT,
    customer_cnpj            TEXT,
    customer_name            TEXT,
    fire_codigo              INTEGER,
    snapshot_json            TEXT,
    check_json               TEXT,
    output_files_json        TEXT,
    db_result_json           TEXT,
    status                   TEXT NOT NULL,
    error                    TEXT,
    portal_status            TEXT NOT NULL DEFAULT 'sent_to_fire',
    sent_to_fire_at          TEXT,
    production_status        TEXT NOT NULL DEFAULT 'none',
    released_at              TEXT,
    released_by              TEXT,
    trace_id                 TEXT,
    state_version            INTEGER NOT NULL DEFAULT 1,
    gestor_order_id          TEXT,
    apontae_order_id         TEXT,
    cliente_override_codigo  INTEGER,
    cliente_override_razao   TEXT,
    cliente_override_at      TEXT,
    cliente_override_by      TEXT,
    fire_status_last_seen    TEXT,
    fire_status_polled_at    TEXT,
    file_sha256              TEXT,
    original_path            TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    environment_id TEXT NOT NULL,
    import_id      TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    detail_json    TEXT,
    created_at     TEXT NOT NULL,
    FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS order_lifecycle_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    environment_id TEXT NOT NULL,
    import_id      TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    source         TEXT NOT NULL,
    payload_json   TEXT,
    trace_id       TEXT,
    occurred_at    TEXT NOT NULL,
    ingested_at    TEXT NOT NULL,
    FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS outbox (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    environment_id   TEXT NOT NULL,
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
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_imports_imported_at    ON imports(imported_at DESC);
CREATE INDEX IF NOT EXISTS idx_imports_customer_cnpj  ON imports(customer_cnpj);
CREATE INDEX IF NOT EXISTS idx_imports_fire_codigo    ON imports(fire_codigo);
CREATE INDEX IF NOT EXISTS idx_imports_status         ON imports(status);
CREATE INDEX IF NOT EXISTS idx_imports_portal_status  ON imports(portal_status);
CREATE INDEX IF NOT EXISTS idx_imports_prod_status    ON imports(production_status);
CREATE INDEX IF NOT EXISTS idx_imports_sha256         ON imports(file_sha256);

CREATE INDEX IF NOT EXISTS idx_audit_import_id ON audit_log(import_id);
CREATE INDEX IF NOT EXISTS idx_audit_created   ON audit_log(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_lifecycle_import_id  ON order_lifecycle_events(import_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_lifecycle_trace_id   ON order_lifecycle_events(trace_id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_event_type ON order_lifecycle_events(event_type, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_outbox_pending   ON outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_outbox_import_id ON outbox(import_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_imports_fire_poll
    ON imports(portal_status, production_status, fire_status_polled_at)
    WHERE fire_codigo IS NOT NULL;
"""

# Schema novo, sem legados — vazio.
COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = ()
