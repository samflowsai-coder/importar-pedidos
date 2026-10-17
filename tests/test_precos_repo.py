"""Cópia local da tabela de preço do Flow (app/persistence/precos_repo.py)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.persistence import precos_repo, router

AGORA = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as c:
        yield c


def _preco(rede, prod, centavos, desde, ate=None, removido=False):
    return {
        "rede_id": rede,
        "fire_produto_id": prod,
        "preco_centavos": centavos,
        "vigente_desde": desde,
        "vigente_ate": ate,
        "removido": removido,
    }


def _lote(conn, **kw):
    base = {"redes": [], "cnpjs": [], "precos": [], "cursor": "c1", "agora": AGORA}
    base.update(kw)
    precos_repo.aplicar_lote(conn, **base)


def _dump(conn):
    return [
        sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall())
        for t in ("preco_rede", "preco_rede_cnpj", "preco_tabela")
    ]


def test_cnpj_completo_vence_raiz(conn):
    _lote(
        conn,
        redes=[
            {"id": "A", "nome": "Rede A", "travada": False, "travada_motivo": None},
            {"id": "B", "nome": "Rede B", "travada": True, "travada_motivo": "x"},
        ],
        cnpjs=[
            {"rede_id": "A", "cnpj": "00063960", "removido": False},
            {"rede_id": "B", "cnpj": "00063960059170", "removido": False},
        ],
    )
    r = precos_repo.resolver_rede(conn, "00.063.960/0591-70")
    assert r.id == "B" and r.travada is True and r.travada_motivo == "x"
    assert precos_repo.resolver_rede(conn, "00063960009408").id == "A"
    assert precos_repo.resolver_rede(conn, "00063960").id == "A"
    assert precos_repo.resolver_rede(conn, "123") is None
    assert precos_repo.resolver_rede(conn, "") is None
    assert precos_repo.resolver_rede(conn, "99999999000100") is None


def test_preco_vigente_respeita_data(conn):
    _lote(
        conn,
        precos=[
            _preco("A", "7", 5200, date(2026, 10, 1)),
            _preco("A", "7", 5460, date(2026, 11, 1)),
        ],
    )
    assert precos_repo.preco_vigente(conn, "A", "7", date(2026, 10, 30)) == 5200
    assert precos_repo.preco_vigente(conn, "A", "7", date(2026, 11, 1)) == 5460
    assert precos_repo.preco_vigente(conn, "A", "7", date(2026, 9, 30)) is None


def test_vigente_ate_exclusivo(conn):
    _lote(conn, precos=[_preco("A", "7", 100, date(2026, 10, 1), date(2026, 10, 10))])
    assert precos_repo.preco_vigente(conn, "A", "7", date(2026, 10, 9)) == 100
    assert precos_repo.preco_vigente(conn, "A", "7", date(2026, 10, 10)) is None


def test_reaplicar_lote_e_idempotente(conn):
    kw = dict(
        redes=[{"id": "A", "nome": "A", "travada": False, "travada_motivo": None}],
        cnpjs=[{"rede_id": "A", "cnpj": "00063960", "removido": False}],
        precos=[_preco("A", "7", 5200, date(2026, 10, 1))],
    )
    _lote(conn, **kw)
    antes = _dump(conn)
    _lote(conn, **kw)
    assert _dump(conn) == antes
    assert len(antes[2]) == 1


def test_removido_some(conn):
    _lote(conn, precos=[_preco("A", "7", 5200, date(2026, 10, 1))])
    _lote(conn, precos=[_preco("A", "7", 5200, date(2026, 10, 1), removido=True)])
    assert precos_repo.preco_vigente(conn, "A", "7", date(2026, 10, 30)) is None


def test_cnpj_removido_some(conn):
    _lote(
        conn,
        redes=[{"id": "A", "nome": "A", "travada": False, "travada_motivo": None}],
        cnpjs=[{"rede_id": "A", "cnpj": "00063960", "removido": False}],
    )
    _lote(conn, cnpjs=[{"rede_id": "A", "cnpj": "00063960", "removido": True}])
    assert precos_repo.resolver_rede(conn, "00063960") is None


def test_estado_sync_grava_cursor_e_hora(conn):
    assert precos_repo.estado_sync(conn, "precos") == (None, None)
    assert precos_repo.ultima_sync_ok(conn) is None
    _lote(conn, cursor="abc")
    assert precos_repo.estado_sync(conn, "precos") == ("abc", AGORA)


def test_ultima_sync_ok_e_a_mais_antiga(conn):
    antes = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)
    _lote(conn, precos=[_preco("A", "7", 1, date(2026, 10, 1))], agora=antes)
    assert precos_repo.estado_sync(conn, "redes") == (None, None)
    assert precos_repo.ultima_sync_ok(conn) is None  # redes nunca sincronizou
    _lote(
        conn,
        redes=[{"id": "A", "nome": "A", "travada": False, "travada_motivo": None}],
    )
    assert precos_repo.ultima_sync_ok(conn) == antes


def test_ultima_sync_ok_none_se_so_redes_sincronizou(conn):
    _lote(
        conn,
        redes=[{"id": "A", "nome": "A", "travada": False, "travada_motivo": None}],
    )
    assert precos_repo.ultima_sync_ok(conn) is None


def test_lote_com_redes_e_precos_marca_os_dois(conn):
    _lote(
        conn,
        redes=[{"id": "A", "nome": "A", "travada": False, "travada_motivo": None}],
        precos=[_preco("A", "7", 1, date(2026, 10, 1))],
    )
    assert precos_repo.ultima_sync_ok(conn) == AGORA


def test_preco_expirado_nao_cai_em_linha_antiga(conn):
    _lote(
        conn,
        precos=[
            _preco("A", "7", 5200, date(2026, 10, 1)),  # aberta
            _preco("A", "7", 5460, date(2026, 11, 1), date(2026, 11, 10)),
        ],
    )
    assert precos_repo.preco_vigente(conn, "A", "7", date(2026, 11, 5)) == 5460
    assert precos_repo.preco_vigente(conn, "A", "7", date(2026, 11, 10)) is None


def test_agora_naive_e_recusado(conn):
    with pytest.raises(ValueError):
        _lote(conn, agora=datetime(2026, 10, 17, 12, 0))


def test_lote_e_atomico(conn):
    with pytest.raises(Exception):
        _lote(
            conn,
            redes=[{"id": "A", "nome": "A", "travada": False, "travada_motivo": None}],
            precos=[{"rede_id": "A"}],  # malformado
        )
    assert precos_repo.resolver_rede(conn, "00063960") is None
    assert conn.execute("SELECT COUNT(*) FROM preco_rede").fetchone()[0] == 0
