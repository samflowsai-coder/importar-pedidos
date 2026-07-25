"""De-para de produto por cliente (produto_depara, db do ambiente)."""

from __future__ import annotations

import pytest

from app.persistence import produto_depara_repo as repo
from app.persistence import router


@pytest.fixture
def env_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as conn:
        yield conn


def _upsert(conn, **over):
    base = dict(
        cliente_cnpj="12.345.678/0001-99",
        chave_tipo="codigo",
        chave_valor=" abc ",
        fire_produto_id="10",
        fire_codigo="10",
        fire_ean="789",
        fire_nome="TENIS",
        criado_em="2026-07-24T10:00:00",
        criado_por="grazi@mm",
    )
    base.update(over)
    repo.upsert(conn, **base)


def test_norm_key_codigo_e_ean():
    assert repo._norm_key("codigo", " abc ") == "ABC"
    assert repo._norm_key("ean", "7.89-0") == "7890"
    assert repo._norm_cnpj("12.345.678/0001-99") == "12345678000199"


def test_upsert_e_lookup_por_codigo(env_conn):
    _upsert(env_conn)
    got = repo.lookup(env_conn, "12345678000199", codigos=["ABC"], eans=[])
    assert ("codigo", "ABC") in got
    assert got[("codigo", "ABC")]["fire_codigo"] == "10"


def test_lookup_normaliza_a_chave_de_busca(env_conn):
    _upsert(env_conn, chave_valor="ABC")
    # busca com valor sujo casa com o gravado normalizado
    got = repo.lookup(env_conn, "12345678000199", codigos=[" abc "], eans=[])
    assert ("codigo", "ABC") in got


def test_colisao_entre_varejistas_resolve_diferente(env_conn):
    _upsert(
        env_conn,
        cliente_cnpj="11111111000100",
        chave_valor="1234",
        fire_produto_id="50",
        fire_codigo="50",
        fire_nome="RIACHUELO X",
    )
    _upsert(
        env_conn,
        cliente_cnpj="22222222000200",
        chave_valor="1234",
        fire_produto_id="60",
        fire_codigo="60",
        fire_nome="CENTAURO Y",
    )
    r1 = repo.lookup(env_conn, "11111111000100", codigos=["1234"], eans=[])
    r2 = repo.lookup(env_conn, "22222222000200", codigos=["1234"], eans=[])
    assert r1[("codigo", "1234")]["fire_codigo"] == "50"
    assert r2[("codigo", "1234")]["fire_codigo"] == "60"


def test_upsert_substitui_mesmo_vinculo(env_conn):
    _upsert(env_conn, chave_valor="ABC", fire_codigo="10")
    _upsert(env_conn, chave_valor="ABC", fire_codigo="99", fire_nome="OUTRO")
    rows = repo.list_for_client(env_conn, "12345678000199")
    assert len(rows) == 1
    assert rows[0]["fire_codigo"] == "99"


def test_delete_desfaz(env_conn):
    _upsert(env_conn)
    rows = repo.list_for_client(env_conn, "12345678000199")
    repo.delete(env_conn, rows[0]["id"])
    assert repo.list_for_client(env_conn, "12345678000199") == []
