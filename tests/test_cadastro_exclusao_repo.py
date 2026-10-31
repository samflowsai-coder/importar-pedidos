"""Backup restaurável das linhas apagadas pelo `excluir` (SQLite do ambiente)."""

import json
from datetime import date
from decimal import Decimal

import pytest

from app.persistence import cadastro_exclusao_repo as repo
from app.persistence import router


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as c:
        yield c


PRODUTO = {
    "SEQ": 709219,
    "DESCRICAO": "MEIA ÇÃO",
    "DATA_CAD": date(2026, 10, 28),
    "PESO": Decimal("0.350"),
    "FOTO": b"\x00\x01\xff",
    "OBS": None,
}
KIT = [{"CODIGO": 90, "CODPRODUTO_PAI": 709219, "CODPRODUTO": 3170, "QTD": Decimal("3")}]


def test_ida_e_volta_preserva_tipos(conn):
    assert repo.guardar(conn, "c1", 2, 709219, PRODUTO, KIT) is True
    linha = repo.buscar(conn, "c1", 2)
    assert linha["seq"] == 709219 and linha["aplicado_em"] is None
    assert repo.restaurar(linha) == (PRODUTO, KIT)


def test_reentrega_nao_duplica_nem_sobrescreve_nunca(conn):
    assert repo.guardar(conn, "c1", 2, 709219, PRODUTO, KIT) is True
    outro = {**PRODUTO, "DESCRICAO": "OUTRO PRODUTO, MESMO CODIGO"}
    assert repo.guardar(conn, "c1", 2, 709219, outro, []) is False
    n = conn.execute("SELECT COUNT(*) FROM cadastro_exclusao_backup").fetchone()[0]
    assert n == 1 and repo.restaurar(repo.buscar(conn, "c1", 2)) == (PRODUTO, KIT)
    repo.marcar_aplicado(conn, "c1", 2)
    assert repo.guardar(conn, "c1", 2, 709219, outro, []) is False
    assert repo.restaurar(repo.buscar(conn, "c1", 2)) == (PRODUTO, KIT)


def test_marcar_aplicado_so_uma_vez(conn):
    repo.guardar(conn, "c1", 2, 709219, PRODUTO, KIT)
    assert repo.marcar_aplicado(conn, "c1", 2) is True
    assert repo.buscar(conn, "c1", 2)["aplicado_em"] is not None
    assert repo.marcar_aplicado(conn, "c1", 2) is False


def test_json_do_backup_e_legivel(conn):
    repo.guardar(conn, "c1", 2, 709219, PRODUTO, KIT)
    linha = repo.buscar(conn, "c1", 2)
    assert json.loads(linha["produto_json"])["SEQ"] == 709219


def test_tipo_que_nao_sabe_serializar_levanta_e_nada_fica(conn):
    with pytest.raises(TypeError):
        repo.guardar(conn, "c1", 2, 1, {"X": object()}, [])
    assert repo.buscar(conn, "c1", 2) is None
