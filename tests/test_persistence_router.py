"""Router de conexões SQLite shared/env."""
from __future__ import annotations

import pytest

from app.persistence import router


def test_shared_db_path_uses_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    p = router.shared_db_path()
    assert p == tmp_path / "app_shared.db"


def test_env_db_path_uses_slug(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    p = router.env_db_path("mm")
    assert p == tmp_path / "app_state_mm.db"


def test_env_db_path_rejects_bad_slug(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        router.env_db_path("../etc/passwd")
    with pytest.raises(ValueError):
        router.env_db_path("MM")  # uppercase
    with pytest.raises(ValueError):
        router.env_db_path("mm prod")  # espaço
    with pytest.raises(ValueError):
        router.env_db_path("")  # vazio
    with pytest.raises(ValueError):
        router.env_db_path("-leading-hyphen")


def test_env_db_path_accepts_valid_slugs(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    # Não deve levantar
    router.env_db_path("mm")
    router.env_db_path("nasmar-2")
    router.env_db_path("a")
    router.env_db_path("emp123")


def test_shared_connect_creates_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {r[0] for r in rows}
    assert "users" in names
    assert "environments" in names
    assert "sessions" in names
    assert "user_invites" in names
    assert "inbound_idempotency" in names
    # operacional NÃO está em shared
    assert "imports" not in names
    assert "outbox" not in names


def test_env_connect_creates_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.env_connect("mm") as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {r[0] for r in rows}
    assert "imports" in names
    assert "outbox" in names
    assert "audit_log" in names
    assert "order_lifecycle_events" in names
    # auth NÃO está em env
    assert "users" not in names
    assert "sessions" not in names


def test_env_connect_isolates_per_slug(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    # cria entradas em DBs diferentes
    with router.env_connect("mm") as conn:
        conn.execute(
            "INSERT INTO imports (id, environment_id, source_filename, imported_at, status)"
            " VALUES ('mm-1', 'env-mm', 'a.pdf', '2026-05-05', 'PARSED')"
        )
    with router.env_connect("nasmar") as conn:
        conn.execute(
            "INSERT INTO imports (id, environment_id, source_filename, imported_at, status)"
            " VALUES ('nm-1', 'env-nm', 'b.pdf', '2026-05-05', 'PARSED')"
        )
    # cada DB só vê o seu
    with router.env_connect("mm") as conn:
        rows = conn.execute("SELECT id FROM imports").fetchall()
    assert {r[0] for r in rows} == {"mm-1"}

    with router.env_connect("nasmar") as conn:
        rows = conn.execute("SELECT id FROM imports").fetchall()
    assert {r[0] for r in rows} == {"nm-1"}


def test_list_env_slugs_empty_when_no_envs(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    assert router.list_env_slugs() == []


def test_list_env_slugs_returns_active(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect() as conn:
        conn.execute(
            "INSERT INTO environments (id, slug, name, watch_dir, output_dir, fb_path,"
            " is_active, created_at, updated_at) VALUES"
            " ('1', 'mm',     'MM',     '/a', '/b', '/c.fdb', 1, 'now', 'now'),"
            " ('2', 'nasmar', 'Nasmar', '/a', '/b', '/c.fdb', 1, 'now', 'now'),"
            " ('3', 'old',    'Old',    '/a', '/b', '/c.fdb', 0, 'now', 'now')"
        )
    assert router.list_env_slugs() == ["mm", "nasmar"]
