"""Tradução da config do ambiente para os kwargs do fdb.connect.

Função pura — sem tocar no driver/lib nativa (roda igual no CI). Com o `fdb`
passamos `host`/`port`/`database` SEPARADOS (não um DSN `host/port:C:\\...`),
justamente pra o drive-letter do Windows não confundir o parser de DSN. TCP
quando há host; embedded quando não há.
"""
from __future__ import annotations

import pytest

from app.erp.connection import _fb_connect_kwargs
from app.erp.exceptions import FirebirdConnectionError


def test_tcp_inclui_host_e_port():
    k = _fb_connect_kwargs({"path": r"C:\Fire\x.fdb", "host": "192.168.15.7", "port": "3050"})
    assert k["host"] == "192.168.15.7"
    assert k["port"] == 3050
    assert k["database"] == r"C:\Fire\x.fdb"


def test_embedded_sem_host_nao_inclui_host_nem_port():
    k = _fb_connect_kwargs({"path": "/tmp/x.fdb"})
    assert "host" not in k and "port" not in k
    assert k["database"] == "/tmp/x.fdb"


def test_porta_default_3050_quando_vazia():
    k = _fb_connect_kwargs({"path": "db", "host": "h", "port": ""})
    assert k["port"] == 3050


def test_defaults_user_e_charset():
    k = _fb_connect_kwargs({"path": "db"})
    assert k["user"] == "SYSDBA"
    assert k["charset"] == "WIN1252"


def test_host_e_valores_normalizados():
    k = _fb_connect_kwargs({"path": "db", "host": "  10.0.0.1  ", "port": "3055", "user": " SYSDBA "})
    assert k["host"] == "10.0.0.1"
    assert k["port"] == 3055
    assert k["user"] == "SYSDBA"


def test_porta_nao_numerica_levanta_firebird_error():
    # campo de porta é texto livre em /admin/ambientes; um typo tem que virar
    # FirebirdConnectionError (contrato de erro do app), não ValueError cru.
    with pytest.raises(FirebirdConnectionError):
        _fb_connect_kwargs({"path": "db", "host": "h", "port": "abc"})


def test_conexao_de_escrita_do_cadastro_tem_lock_timeout_em_toda_transacao(monkeypatch):
    """READ COMMITTED + WAIT com lock timeout de 10 s: um SEQ aberto na tela do Fire
    não trava o gravador sem limite. O TPB vale também depois de cada commit/rollback."""
    import struct
    from contextlib import contextmanager

    import fdb

    from app.erp.connection import FirebirdConnection

    class _Tr:
        default_tpb = None
        default_action = "commit"  # o padrão do fdb

    class _Conn:
        def __init__(self):
            self.main_transaction, self.begins = _Tr(), []

        def begin(self, tpb=None):
            self.begins.append(tpb)

    conn = _Conn()

    @contextmanager
    def _falso(self, cfg, **kw):
        yield conn

    monkeypatch.setattr(FirebirdConnection, "_connect_with", _falso)
    with FirebirdConnection().connect_escrita_cadastro({"path": "x"}) as c:
        assert c is conn
    esperado = bytes([
        fdb.isc_tpb_version3, fdb.isc_tpb_write, fdb.isc_tpb_read_committed,
        fdb.isc_tpb_rec_version, fdb.isc_tpb_wait, fdb.isc_tpb_lock_timeout,
    ]) + struct.pack("<bI", 4, 10)
    assert conn.begins == [esperado]
    assert conn.main_transaction.default_tpb == esperado
    # Fechar a conexão com transação aberta desfaz, nunca comita.
    assert conn.main_transaction.default_action == "rollback"


def test_leitura_bloqueante_espera_registro_pendente(monkeypatch):
    """Recuperação do cadastro: WRITE + READ COMMITTED + NO_REC_VERSION + WAIT com lock
    timeout (WRITE porque READ ONLY pode ser pré-comitada e ignorar o NO_REC_VERSION).
    Um INSERT sem commit de outra transação vira conflito, não ausência; a saída desfaz."""
    import struct
    from contextlib import contextmanager

    import fdb

    from app.erp.connection import FirebirdConnection

    class _Tr:
        default_action = "commit"  # o padrão do fdb

    class _Conn:
        def __init__(self):
            self.begins, self.main_transaction = [], _Tr()

        def begin(self, tpb=None):
            self.begins.append(tpb)

    conn, kws = _Conn(), []

    @contextmanager
    def _falso(self, cfg, **kw):
        kws.append(kw)
        yield conn

    monkeypatch.setattr(FirebirdConnection, "_connect_with", _falso)
    with FirebirdConnection().connect_leitura_bloqueante({"path": "x"}) as c:
        assert c is conn
    assert conn.begins == [bytes([
        fdb.isc_tpb_version3, fdb.isc_tpb_write, fdb.isc_tpb_read_committed,
        fdb.isc_tpb_no_rec_version, fdb.isc_tpb_wait, fdb.isc_tpb_lock_timeout,
    ]) + struct.pack("<bI", 4, 10)]
    assert kws == [{"commit_na_saida": False}]  # saída sempre rollback, nunca commit
    assert conn.main_transaction.default_action == "rollback"
    with FirebirdConnection().connect_leitura_bloqueante({"path": "x"}, lock_timeout=3):
        pass
    assert conn.begins[-1].endswith(struct.pack("<bI", 4, 3))


@pytest.mark.firebird_stub_proprio
def test_conexao_de_escrita_sai_com_rollback_nunca_commit(monkeypatch):
    """O que sobrou sem commit do gravador (rollback que falhou) não tem prova: a saída
    desfaz. Os outros modos continuam saindo com commit."""
    import fdb

    from app.erp.connection import FirebirdConnection

    class _Tr:
        default_tpb = None

    class _Conn:
        def __init__(self):
            self.main_transaction, self.chamadas = _Tr(), []

        def begin(self, tpb=None):
            pass

        def cursor(self):
            return self

        def execute(self, sql, params=()):
            self.chamadas.append("execute")

        def commit(self):
            self.chamadas.append("commit")

        def rollback(self):
            self.chamadas.append("rollback")

        def close(self):
            self.chamadas.append("close")

    conn = _Conn()
    monkeypatch.setattr(FirebirdConnection, "_configure_library", lambda self: None)
    monkeypatch.setattr(fdb, "connect", lambda **k: conn)
    with FirebirdConnection().connect_escrita_cadastro({"path": "x"}) as c:
        c.cursor().execute("INSERT INTO PRODUTOS (SEQ) VALUES (?)", (1,))
    assert conn.chamadas == ["execute", "rollback", "close"]

    outra = _Conn()
    monkeypatch.setattr(fdb, "connect", lambda **k: outra)
    with FirebirdConnection().connect_with_config({"path": "x"}):
        pass
    assert outra.chamadas == ["commit", "close"]
