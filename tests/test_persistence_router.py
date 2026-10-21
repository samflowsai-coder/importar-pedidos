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


def test_drop_do_modelo_anterior_de_preco_roda_uma_vez_por_banco(tmp_path, monkeypatch):
    """O DROP das tabelas de preço do modelo anterior roda só no 1º boot do
    banco (marcador em `schema_migracao`): um banco já migrado não perde uma
    tabela com esse nome nos boots seguintes."""
    import sqlite3

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    caminho = router.env_db_path("mm")
    velho = sqlite3.connect(caminho)
    velho.executescript(
        "CREATE TABLE preco_rede (id TEXT); INSERT INTO preco_rede VALUES ('velha');"
        "CREATE TABLE preco_aprovacao (id TEXT);"
    )
    velho.commit()
    velho.close()

    router.reset_init_cache()
    with router.env_connect("mm") as conn:
        nomes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "preco_rede" not in nomes and "preco_aprovacao" not in nomes
        marcas = [r[0] for r in conn.execute("SELECT nome FROM schema_migracao")]
        assert len(marcas) == 1
        conn.execute("CREATE TABLE preco_tabela (dado TEXT)")
        conn.execute("INSERT INTO preco_tabela VALUES ('fica')")

    router.reset_init_cache()  # novo boot
    with router.env_connect("mm") as conn:
        assert [r[0] for r in conn.execute("SELECT dado FROM preco_tabela")] == ["fica"]
        assert conn.execute("SELECT COUNT(*) FROM schema_migracao").fetchone()[0] == 1


def test_migracao_uma_vez_com_dois_processos_no_primeiro_boot(tmp_path):
    """Dois processos sobem juntos num banco novo: o segundo grava o marcador
    entre a leitura e o INSERT do primeiro. Nenhum quebra (IntegrityError) e o
    marcador fica uma vez só."""
    import sqlite3

    caminho = tmp_path / "app_state_mm.db"
    migs = (("m1", ("CREATE TABLE IF NOT EXISTS t1 (x INTEGER)",)),)
    b = sqlite3.connect(caminho, timeout=5.0)

    class _ProcessoA:
        """Conexão do processo A: logo depois de ler os nomes fora de uma
        transação, o processo B roda a migração inteira."""

        def __init__(self):
            self.conn = sqlite3.connect(caminho, timeout=5.0)
            self.intercalou = False

        @property
        def in_transaction(self):
            return self.conn.in_transaction

        def execute(self, sql, *args):
            cur = self.conn.execute(sql, *args)
            if (
                not self.intercalou
                and "SELECT nome FROM schema_migracao" in sql
                and not self.conn.in_transaction
            ):
                linhas = cur.fetchall()
                self.intercalou = True
                router._apply_migracoes_uma_vez(b, migs)
                return iter(linhas)
            return cur

    a = _ProcessoA()
    router._apply_migracoes_uma_vez(a, migs)
    router._apply_migracoes_uma_vez(b, migs)
    assert b.execute("SELECT COUNT(*) FROM schema_migracao").fetchone()[0] == 1
    a.conn.close()
    b.close()


def test_migracao_uma_vez_cria_o_marcador_sozinha(tmp_path):
    import sqlite3

    c = sqlite3.connect(tmp_path / "x.db")
    router._apply_migracoes_uma_vez(c, (("m1", ("CREATE TABLE t1 (x INTEGER)",)),))
    router._apply_migracoes_uma_vez(c, (("m1", ("CREATE TABLE t1 (x INTEGER)",)),))
    assert [r[0] for r in c.execute("SELECT nome FROM schema_migracao")] == ["m1"]
    c.close()


def test_migracao_uma_vez_espera_transacao_de_escrita_de_outro_processo(tmp_path):
    """Outro processo segura uma escrita (ex.: o 1º boot dele no meio da
    migração): `BEGIN IMMEDIATE` espera o lock (busy timeout), depois relê os
    nomes e não roda nem grava de novo."""
    import sqlite3
    import threading

    caminho = tmp_path / "app_state_mm.db"
    migs = (("m1", ("CREATE TABLE t1 (x INTEGER)",)),)
    outro = sqlite3.connect(caminho, timeout=5.0, check_same_thread=False)
    outro.execute(
        "CREATE TABLE IF NOT EXISTS schema_migracao"
        " (nome TEXT PRIMARY KEY, aplicada_em TEXT NOT NULL)"
    )
    outro.execute("BEGIN IMMEDIATE")
    outro.execute("CREATE TABLE t1 (x INTEGER)")
    outro.execute("INSERT INTO schema_migracao VALUES ('m1', 'agora')")
    libera = threading.Timer(0.3, lambda: outro.execute("COMMIT"))
    libera.start()

    eu = sqlite3.connect(caminho, timeout=5.0)
    router._apply_migracoes_uma_vez(eu, migs)  # sem 'table t1 already exists'
    libera.join()

    assert eu.execute("SELECT COUNT(*) FROM schema_migracao").fetchone()[0] == 1
    eu.close()
    outro.close()
