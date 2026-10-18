"""Ferramentas SÓ LEITURA da P0 (tools/perfil_cadastro_fire.py e tools/produto_no_fire.py).

Só as funções puras: escolha de tabelas/colunas, montagem do SQL e perfil. Nada
conecta no Firebird."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _carrega(nome: str):
    spec = importlib.util.spec_from_file_location(nome, _TOOLS / f"{nome}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


perfil_tool = _carrega("perfil_cadastro_fire")
rastro = _carrega("produto_no_fire")


def test_perfil_coluna_sempre_preenchida_e_coluna_sempre_nula():
    linhas = [
        {"X": "A", "Y": None},
        {"X": "A", "Y": None},
        {"X": "A", "Y": None},
    ]
    r = {p["coluna"]: p for p in perfil_tool.perfil(["X", "Y"], linhas)}
    assert r["X"] == {"coluna": "X", "preenchidos": 3, "mais_comum": "A"}
    assert r["Y"]["preenchidos"] == 0
    assert r["Y"]["mais_comum"] is None


def test_perfil_vazio_e_espaco_nao_contam_e_mais_comum_ganha():
    linhas = [{"X": "A"}, {"X": "B"}, {"X": "B"}, {"X": "  "}, {"X": ""}]
    (p,) = perfil_tool.perfil(["X"], linhas)
    assert p["preenchidos"] == 3
    assert p["mais_comum"] == "B"


def test_perfil_zero_e_valor_preenchido():
    (p,) = perfil_tool.perfil(["X"], [{"X": 0}, {"X": 0}])
    assert p["preenchidos"] == 2
    assert p["mais_comum"] == 0


def test_perfil_sem_linhas():
    (p,) = perfil_tool.perfil(["X"], [])
    assert p == {"coluna": "X", "preenchidos": 0, "mais_comum": None}


CATALOGO = [
    ("PRODUTOS", "SEQ"),
    ("NOTAPROD", "CODPRODUTO"),
    ("NOTAPROD", "VALOR"),
    ("PRODUTOS_KIT", "CODPRODUTO_PAI"),
    ("PRODUTOS_KIT", "CODPRODUTO"),
    ("RDB$RELATIONS", "SEQ"),
    ("MON$STATEMENTS", "PRODUTO"),
    ("SEC$USERS", "SEQ"),
    ("CLIENTES", "NOME"),
]


def test_alvos_filtra_colunas_candidatas_e_tabelas_de_sistema():
    alvos = rastro.escolher_alvos(CATALOGO)
    assert alvos == [
        ("NOTAPROD", "CODPRODUTO"),
        ("PRODUTOS", "SEQ"),
        ("PRODUTOS_KIT", "CODPRODUTO"),
        ("PRODUTOS_KIT", "CODPRODUTO_PAI"),
    ]


def test_sql_do_rastro_e_select_limitado_e_parametrizado():
    sql = rastro.montar_sql("NOTAPROD", "CODPRODUTO")
    assert sql == 'SELECT FIRST 50 * FROM "NOTAPROD" WHERE "CODPRODUTO" = ?'


@pytest.mark.parametrize(
    "nome", ["notaprod", "NOTA PROD", 'X"; DROP TABLE Y; --', "1ABC", "", "A-B"]
)
def test_nome_de_tabela_invalido_e_recusado(nome):
    with pytest.raises(ValueError):
        rastro.montar_sql(nome, "SEQ")
    with pytest.raises(ValueError):
        rastro.montar_sql("PRODUTOS", nome)


def test_so_select_passa():
    rastro.so_select("  select 1 from rdb$database")
    with pytest.raises(AssertionError):
        rastro.so_select("DELETE FROM PRODUTOS")
    perfil_tool.so_select("SELECT 1 FROM RDB$DATABASE")
    with pytest.raises(AssertionError):
        perfil_tool.so_select("UPDATE PRODUTOS SET GRUPO = 1")


def test_alvo_do_catalogo_com_nome_invalido_e_recusado_no_sql():
    # Mesmo vindo do catálogo, o identificador é validado antes de virar texto de SQL.
    alvos = rastro.escolher_alvos([("T-RUIM", "SEQ")])
    with pytest.raises(ValueError):
        rastro.montar_sql(*alvos[0])
