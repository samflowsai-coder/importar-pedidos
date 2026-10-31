"""Quais colunas do Fire apontam para PRODUTOS.SEQ (guarda de uso do `excluir`)."""

import json
from pathlib import Path

import pytest

from app.erp import produto_referencias as pr

_FIXTURE = Path(__file__).parent / "fixtures" / "fire_catalogo_colunas_produto.json"


@pytest.fixture(scope="module")
def catalogo_real():
    dados = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return [tuple(c) for c in dados["colunas"]]


def test_so_select_recusa_o_resto():
    assert pr.so_select("SELECT 1 FROM RDB$DATABASE")
    with pytest.raises(RuntimeError):
        pr.so_select("DELETE FROM PRODUTOS")


def test_ident_so_aceita_nome_de_coluna():
    assert pr._ident("CODPRODUTO") == '"CODPRODUTO"'
    with pytest.raises(ValueError):
        pr._ident('X"; DROP TABLE PRODUTOS; --')


def test_alvos_do_catalogo_real_cobrem_o_que_a_varredura_de_28_10_provou(catalogo_real):
    alvos = pr.colunas_de_uso(catalogo_real)
    tabelas = {t for t, _ in alvos}
    assert pr.TABELAS_OBRIGATORIAS <= tabelas
    assert len(alvos) >= 60  # 69 colunas inteiras com PROD no nome na .7 em 31/10/2026
    # a chave de cada tabela não é referência a produto; o pai do kit é apagado junto
    assert ("PRODUTOS", "SEQ") not in alvos
    assert ("PRODUTOS_KIT", "CODPRODUTO_PAI") not in alvos
    # referências de produto para produto dentro de PRODUTOS entram
    assert ("PRODUTOS", "CODPRODFICHA") in alvos
    assert ("PRODUTOS", "CONFEC_COD_PROD_NF") in alvos
    assert ("NOTAPROD", "CODPRODUTO_ORIGEM") in alvos
    assert ("PRODUTOS_ENG", "CODPRODUTO_ENG") in alvos


def test_alvos_ignoram_texto_sistema_e_colunas_sem_prod():
    catalogo = [
        ("ESTOQUE", "CODPRODUTO", 8),
        ("ESTOQUE", "OBS_PRODUTO", 37),  # VARCHAR
        ("ESTOQUE", "QTD", 8),
        ("RDB$X", "CODPRODUTO", 8),
        ("MON$Y", "CODPRODUTO", 8),
        ("OUTRA", "CODPRODUTO", 16),
    ]
    assert pr.colunas_de_uso(catalogo) == [("ESTOQUE", "CODPRODUTO"), ("OUTRA", "CODPRODUTO")]


def test_faltando_tabela_obrigatoria_levanta():
    catalogo = [(t, "CODPRODUTO", 8) for t in pr.TABELAS_OBRIGATORIAS if t != "NOTAPROD"]
    alvos = pr.colunas_de_uso(catalogo)
    with pytest.raises(pr.CatalogoIncompletoError) as e:
        pr.exige_tabelas_obrigatorias(alvos)
    assert "NOTAPROD" in str(e.value)


def test_sql_de_uso_e_so_select_parametrizado():
    sql = pr.sql_de_uso("ESTOQUE", "CODPRODUTO")
    assert sql == 'SELECT FIRST 1 1 FROM "ESTOQUE" WHERE "CODPRODUTO" = ?'
    pr.so_select(sql)
