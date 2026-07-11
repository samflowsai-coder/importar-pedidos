"""Cópia local do catálogo do Fire (catalogo_fire no db do ambiente).

O sync SEMPRE persiste a extração aqui (snapshot substitutivo); o envio ao
Flow é gated por flowpcp_catalogo_push. Esta cópia é o "manter no importador".
"""
from __future__ import annotations

import pytest

from app.erp.catalog_extract import ProdutoFireDTO
from app.persistence import catalogo_fire_repo, router


@pytest.fixture
def env_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as conn:
        yield conn


def _dto(seq: str, nome: str = "PRODUTO", ativo: bool = True) -> ProdutoFireDTO:
    return ProdutoFireDTO(
        fire_produto_id=seq, codigo=seq, nome=nome,
        unidade="PAR", ean=None, ativo=ativo, tipo="simples",
    )


def test_replace_all_persiste_snapshot(env_conn):
    n = catalogo_fire_repo.replace_all(
        env_conn, [_dto("1", "MEIA A"), _dto("2", "MEIA B", ativo=False)],
        extraido_em="2026-07-11T10:00:00Z",
    )
    assert n == 2
    rows = catalogo_fire_repo.list_all(env_conn)
    assert len(rows) == 2
    r1 = next(r for r in rows if r["fire_produto_id"] == "1")
    assert r1["nome"] == "MEIA A"
    assert r1["ativo"] == 1
    assert r1["extraido_em"] == "2026-07-11T10:00:00Z"
    r2 = next(r for r in rows if r["fire_produto_id"] == "2")
    assert r2["ativo"] == 0


def test_replace_all_e_substitutivo(env_conn):
    catalogo_fire_repo.replace_all(env_conn, [_dto("1"), _dto("2")], extraido_em="t1")
    catalogo_fire_repo.replace_all(env_conn, [_dto("3", "NOVO")], extraido_em="t2")
    rows = catalogo_fire_repo.list_all(env_conn)
    assert [r["fire_produto_id"] for r in rows] == ["3"]
    assert catalogo_fire_repo.count(env_conn) == 1
