from __future__ import annotations

from app.persistence import flowpcp_repo as repo
from app.persistence.schema_env import TABLES_SQL


def _init(conn):
    conn.executescript(TABLES_SQL)
    return conn


def test_cursor_roundtrip(tmp_env_db):
    conn = _init(tmp_env_db)
    assert repo.get_last_cursor(conn) is None
    repo.save_last_cursor(conn, "2026-06-22T14:00:00.000Z")
    assert repo.get_last_cursor(conn) == "2026-06-22T14:00:00.000Z"
    repo.save_last_cursor(conn, "2026-06-22T15:00:00.000Z")
    assert repo.get_last_cursor(conn) == "2026-06-22T15:00:00.000Z"


def test_attempts_increment(tmp_env_db):
    conn = _init(tmp_env_db)
    assert repo.get_attempts_count(conn, "dec-1") == 0
    assert repo.register_attempt(conn, "dec-1") == 1
    assert repo.register_attempt(conn, "dec-1") == 2
    assert repo.get_attempts_count(conn, "dec-1") == 2


def test_mark_reconciliada(tmp_env_db):
    conn = _init(tmp_env_db)
    repo.register_attempt(conn, "dec-1")
    repo.mark_reconciliada(conn, "dec-1", "data_atualizada")
    row = conn.execute(
        "SELECT acao_executada, reconciliado_em FROM flowpcp_decisoes_mapping WHERE decisao_id=?",
        ("dec-1",),
    ).fetchone()
    assert row["acao_executada"] == "data_atualizada"
    assert row["reconciliado_em"] is not None
