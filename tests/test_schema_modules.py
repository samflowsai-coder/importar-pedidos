"""Schemas shared e env separados — verifica que cada módulo expõe o esperado."""
from __future__ import annotations


def test_schema_shared_exports():
    from app.persistence import schema_shared

    assert hasattr(schema_shared, "TABLES_SQL")
    assert hasattr(schema_shared, "INDEXES_SQL")
    assert hasattr(schema_shared, "COLUMN_MIGRATIONS")
    # auth/env metadata vivem aqui
    assert "users" in schema_shared.TABLES_SQL
    assert "environments" in schema_shared.TABLES_SQL
    assert "sessions" in schema_shared.TABLES_SQL
    assert "user_invites" in schema_shared.TABLES_SQL
    assert "inbound_idempotency" in schema_shared.TABLES_SQL
    # dados operacionais NÃO vivem aqui
    assert "imports" not in schema_shared.TABLES_SQL
    assert "outbox" not in schema_shared.TABLES_SQL


def test_schema_env_exports():
    from app.persistence import schema_env

    assert hasattr(schema_env, "TABLES_SQL")
    assert hasattr(schema_env, "INDEXES_SQL")
    assert hasattr(schema_env, "COLUMN_MIGRATIONS")
    # operacional vive aqui
    assert "imports" in schema_env.TABLES_SQL
    assert "outbox" in schema_env.TABLES_SQL
    assert "audit_log" in schema_env.TABLES_SQL
    assert "order_lifecycle_events" in schema_env.TABLES_SQL
    # auth NÃO vive aqui
    assert "users" not in schema_env.TABLES_SQL
    assert "sessions" not in schema_env.TABLES_SQL
