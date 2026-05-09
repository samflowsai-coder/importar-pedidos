"""Tests for app.persistence.repo (SQLite import history)."""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest

from app.persistence import db, repo


@pytest.fixture
def sqlite_tmp(tmp_path: Path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _entry(**overrides) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "source_filename": "pedido.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "12345",
        "customer": "ACME LTDA",
        "customer_cnpj": "00000000000100",
        "output_files": [{"name": "out.xlsx", "path": "/tmp/out.xlsx"}],
        "db_result": {"order_number": "12345", "items_inserted": 3, "fire_codigo": 99},
        "fire_codigo": 99,
        "snapshot": {"header": {"order_number": "12345", "customer_cnpj": "00000000000100"}},
        "status": "success",
        "error": None,
    }
    base.update(overrides)
    return base


def test_insert_and_get_roundtrip(sqlite_tmp):
    e = _entry()
    repo.insert_import(e)
    got = repo.get_import(e["id"])
    assert got is not None
    assert got["order_number"] == "12345"
    assert got["customer"] == "ACME LTDA"
    assert got["fire_codigo"] == 99
    assert got["output_files"][0]["name"] == "out.xlsx"
    assert got["db_result"]["items_inserted"] == 3
    assert got["snapshot"]["header"]["order_number"] == "12345"
    assert got["production_status"] == "none"


def test_insert_is_idempotent_upsert(sqlite_tmp):
    e = _entry(status="success")
    repo.insert_import(e)
    e["status"] = "error"
    e["error"] = "boom"
    repo.insert_import(e)
    got = repo.get_import(e["id"])
    assert got["status"] == "error"
    assert got["error"] == "boom"


def test_list_orders_by_imported_at_desc(sqlite_tmp):
    for i, ts in enumerate(["2026-01-01T10:00:00", "2026-03-01T10:00:00", "2026-02-01T10:00:00"]):
        repo.insert_import(_entry(imported_at=ts, source_filename=f"f{i}.pdf"))
    rows = repo.list_imports(limit=10)
    timestamps = [r["imported_at"] for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)


def test_list_filters_by_status(sqlite_tmp):
    repo.insert_import(_entry(status="success"))
    repo.insert_import(_entry(status="error"))
    assert repo.count_imports(status="success") == 1
    assert repo.count_imports(status="error") == 1
    assert {r["status"] for r in repo.list_imports(status="error")} == {"error"}


def test_list_search_by_customer_cnpj_or_order(sqlite_tmp):
    repo.insert_import(_entry(order_number="AAA", customer="Riachuelo"))
    repo.insert_import(_entry(order_number="BBB", customer="Beira Rio"))

    assert repo.count_imports(customer_search="Riachuelo") == 1
    assert repo.count_imports(customer_search="BBB") == 1
    assert repo.count_imports(customer_search="nao-existe") == 0


def test_list_search_is_parameterized_and_safe(sqlite_tmp):
    repo.insert_import(_entry(customer="ACME", order_number="PED-1"))
    # Classic SQL-injection attempt: should find nothing, not error out.
    needle = "'; DROP TABLE imports; --"
    assert repo.count_imports(customer_search=needle) == 0
    # Table still works
    assert repo.count_imports() == 1


def test_pagination(sqlite_tmp):
    for i in range(7):
        repo.insert_import(_entry(imported_at=f"2026-04-{22 - i:02d}T10:00:00"))
    page1 = repo.list_imports(limit=3, offset=0)
    page2 = repo.list_imports(limit=3, offset=3)
    assert len(page1) == 3
    assert len(page2) == 3
    assert page1[-1]["imported_at"] > page2[0]["imported_at"]


def test_list_caps_limit():
    # Even if caller passes huge limit, repo caps to MAX_PAGE_SIZE
    # This is a static check — no DB needed.
    from app.persistence.repo import _MAX_PAGE_SIZE
    assert _MAX_PAGE_SIZE == 500


def test_derives_cnpj_from_snapshot_when_missing(sqlite_tmp):
    e = _entry()
    e.pop("customer_cnpj", None)
    repo.insert_import(e)
    got = repo.get_import(e["id"])
    assert got["customer_cnpj"] == "00000000000100"


def test_audit_log_appended_and_listed(sqlite_tmp):
    e = _entry()
    repo.insert_import(e)
    repo.append_audit(e["id"], "imported", {"source": "commit", "items": 3})
    repo.append_audit(e["id"], "released_for_production", {"by": "user"})

    events = repo.list_audit(e["id"])
    assert len(events) == 2
    assert events[0]["event_type"] == "released_for_production"
    assert events[0]["detail"]["by"] == "user"
    assert events[1]["event_type"] == "imported"
    assert events[1]["detail"]["items"] == 3


def test_audit_cascade_on_import_delete(sqlite_tmp):
    e = _entry()
    repo.insert_import(e)
    repo.append_audit(e["id"], "imported", None)
    with db.connect() as conn:
        conn.execute("DELETE FROM imports WHERE id = ?", (e["id"],))
    assert repo.list_audit(e["id"]) == []


def test_set_client_override_persists_and_get_returns_them(sqlite_tmp):
    e = _entry()
    repo.insert_import(e)

    fresh = repo.get_import(e["id"])
    assert fresh["cliente_override_codigo"] is None
    assert fresh["cliente_override_razao"] is None
    assert fresh["cliente_override_at"] is None
    assert fresh["cliente_override_by"] is None

    repo.set_client_override(e["id"], codigo=4242, razao="ACME COMERCIO LTDA")

    got = repo.get_import(e["id"])
    assert got["cliente_override_codigo"] == 4242
    assert got["cliente_override_razao"] == "ACME COMERCIO LTDA"
    assert got["cliente_override_at"]  # ISO timestamp present
    assert got["cliente_override_by"] is None  # placeholder until v5 auth


def test_set_client_override_appears_in_list_imports(sqlite_tmp):
    e = _entry()
    repo.insert_import(e)
    repo.set_client_override(e["id"], codigo=99, razao="FOO LTDA")
    rows = repo.list_imports(limit=10)
    target = next(r for r in rows if r["id"] == e["id"])
    assert target["cliente_override_codigo"] == 99
    assert target["cliente_override_razao"] == "FOO LTDA"


def test_insert_import_does_not_clobber_client_override(sqlite_tmp):
    e = _entry()
    repo.insert_import(e)
    repo.set_client_override(e["id"], codigo=7777, razao="OVERRIDE LTDA", user="alice@example.com")

    # Re-upsert (e.g. retry path) — must NOT wipe the override columns.
    e_again = _entry(id=e["id"], customer="OUTRO NOME")
    repo.insert_import(e_again)

    got = repo.get_import(e["id"])
    assert got["cliente_override_codigo"] == 7777
    assert got["cliente_override_razao"] == "OVERRIDE LTDA"
    assert got["cliente_override_by"] == "alice@example.com"
    # Sanity: other clobberable column did update.
    assert got["customer"] == "OUTRO NOME"


def test_set_client_override_last_write_wins(sqlite_tmp):
    e = _entry()
    repo.insert_import(e)
    repo.set_client_override(e["id"], codigo=1, razao="PRIMEIRA")
    repo.set_client_override(e["id"], codigo=2, razao="SEGUNDA")
    got = repo.get_import(e["id"])
    assert got["cliente_override_codigo"] == 2
    assert got["cliente_override_razao"] == "SEGUNDA"


def test_schema_includes_sem_preco_ack_columns(sqlite_tmp):
    """schema_env.TABLES_SQL deve incluir as 3 colunas do sidecar de ack."""
    from app.persistence import db
    with db.connect() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(imports)").fetchall()}
    assert "sem_preco_ack_by" in cols
    assert "sem_preco_ack_at" in cols
    assert "sem_preco_ack_items" in cols


def test_column_migration_is_idempotent(tmp_path):
    """_ensure_schema executa COLUMN_MIGRATIONS em DB legada e é idempotente.

    Simula uma DB de produção existente (sem as 3 colunas sem_preco_ack_*),
    confirma que _ensure_schema as adiciona na primeira chamada, e que uma
    segunda chamada (após limpar o cache) não levanta erro (ALTER TABLE IF NOT
    EXISTS não existe no SQLite — a própria _apply_column_migrations checa
    via PRAGMA table_info antes de cada ALTER).
    """
    import sqlite3

    from app.persistence import schema_env
    from app.persistence import router as persistence_router
    from app.persistence.router import _ensure_schema

    # 1. Criar DB legada com schema antigo — todas as colunas atuais EXCETO as 3
    #    novas de ack (simula DB de produção antes da migração).
    db_path = tmp_path / "app_state_legacy.db"
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.executescript(
        """
        CREATE TABLE imports (
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

        CREATE TABLE IF NOT EXISTS product_sync_state (
            seq             INTEGER PRIMARY KEY,
            content_hash    TEXT NOT NULL,
            last_synced_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS component_sync_state (
            codigo          INTEGER PRIMARY KEY,
            content_hash    TEXT NOT NULL,
            last_synced_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS product_sync_runs (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            environment_id           TEXT NOT NULL,
            sync_id                  TEXT NOT NULL UNIQUE,
            trigger                  TEXT NOT NULL,
            started_at               TEXT NOT NULL,
            finished_at              TEXT,
            status                   TEXT NOT NULL,
            delta_count_produtos     INTEGER NOT NULL DEFAULT 0,
            delta_count_componentes  INTEGER NOT NULL DEFAULT 0,
            delta_count_tombstones   INTEGER NOT NULL DEFAULT 0,
            applied_count            INTEGER NOT NULL DEFAULT 0,
            errors_json              TEXT,
            trace_id                 TEXT
        );
        """
    )
    legacy_conn.commit()
    legacy_conn.close()

    # 2. Confirmar que as colunas de ack NÃO estão presentes ainda
    legacy_check = sqlite3.connect(db_path)
    cols_before = {row[1] for row in legacy_check.execute("PRAGMA table_info(imports)").fetchall()}
    legacy_check.close()
    assert "sem_preco_ack_by" not in cols_before
    assert "sem_preco_ack_at" not in cols_before
    assert "sem_preco_ack_items" not in cols_before

    # 3. Primeira chamada: deve rodar TABLES_SQL (no-op por IF NOT EXISTS) e
    #    depois _apply_column_migrations, que adiciona as 3 colunas ausentes
    persistence_router.reset_init_cache()
    _ensure_schema(db_path, schema_env)

    conn_after = sqlite3.connect(db_path)
    cols_after = {row[1] for row in conn_after.execute("PRAGMA table_info(imports)").fetchall()}
    conn_after.close()
    assert {"sem_preco_ack_by", "sem_preco_ack_at", "sem_preco_ack_items"} <= cols_after

    # 4. Segunda chamada (após reset do cache): idempotência no nível SQL —
    #    _apply_column_migrations checa PRAGMA table_info e pula ALTERs já feitos
    persistence_router.reset_init_cache()
    _ensure_schema(db_path, schema_env)  # não deve levantar erro

    conn_final = sqlite3.connect(db_path)
    cols_final = {row[1] for row in conn_final.execute("PRAGMA table_info(imports)").fetchall()}
    conn_final.close()
    assert {"sem_preco_ack_by", "sem_preco_ack_at", "sem_preco_ack_items"} <= cols_final
