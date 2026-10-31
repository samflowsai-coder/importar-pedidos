"""Colunas do Fire que apontam para `PRODUTOS.SEQ` (guarda de uso do `excluir`).

O Fire não tem chave estrangeira para produto: a única prova de que um produto não
tem uso é procurar o SEQ em toda coluna que pode guardá-lo. A lista vem do catálogo
(`RDB$RELATION_FIELDS`), nunca escrita à mão: tabela ou coluna nova do Fire entra
sozinha. A regra é larga de propósito: falso positivo só recusa a exclusão (o operador
vê a tabela na mensagem); falso negativo apagaria um produto que alguém ainda usa.

Todo SQL daqui é SELECT, com identificador do catálogo validado e entre aspas, e o
SEQ sempre como parâmetro. Também é a fonte do catálogo/`so_select`/`_ident` da
ferramenta `tools/produto_no_fire.py`.
"""

from __future__ import annotations

import re

TIPOS_INTEIROS = frozenset({7, 8, 16})  # SMALLINT, INTEGER, BIGINT
PREFIXOS_SISTEMA = ("RDB$", "MON$", "SEC$")
SQL_CATALOGO = (
    "SELECT TRIM(RF.RDB$RELATION_NAME), TRIM(RF.RDB$FIELD_NAME), F.RDB$FIELD_TYPE "
    "FROM RDB$RELATION_FIELDS RF "
    "JOIN RDB$FIELDS F ON F.RDB$FIELD_NAME = RF.RDB$FIELD_SOURCE "
    "JOIN RDB$RELATIONS R ON R.RDB$RELATION_NAME = RF.RDB$RELATION_NAME "
    "WHERE COALESCE(R.RDB$SYSTEM_FLAG, 0) = 0 AND R.RDB$VIEW_BLR IS NULL"
)
# Colunas que guardam produto sem "PROD" no nome: material da ficha de confecção, tecido e fio
# (também são linhas de PRODUTOS) e variante. Sem chave estrangeira no Fire (0 FKs para
# PRODUTOS na .7, 31/10/2026), o nome é a única pista. Revisadas e deixadas de fora de
# propósito (não guardam SEQ): CODFAMILIA_TECIDO, CONFEC_TIPO_COMP_TECIDOS, TECIDO_CODGAVETA,
# GRUPOTECIDO*, CODITEM*/COD_ITEM* (itens de nota, venda e OP), ORIG_CODIGO* (documento de origem).
_EXTRA_NOME = re.compile(r"CODMATERIAL|CODTECIDO|CODVARIANTE|CODFIO")
# Tabelas que a varredura de 28/10/2026 provou ter coluna de produto. Se o catálogo lido
# não trouxer alguma, a leitura está incompleta e nada é excluído.
TABELAS_OBRIGATORIAS = frozenset(
    {"ESTOQUE", "CORPO_VENDAS", "NOTAPROD", "OP_ITENSPROD", "PRODUTOS_ENG", "PRODUTOS_EMBALAGEM"}
)
# Fora da guarda de uso: a chave da própria tabela; o pai do kit (apagado junto com o kit);
# o filho do kit tem guarda própria (`ligado_a_kit`).
_FORA = frozenset(
    {("PRODUTOS", "SEQ"), ("PRODUTOS_KIT", "CODPRODUTO_PAI"), ("PRODUTOS_KIT", "CODPRODUTO")}
)
_SO_SELECT = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_IDENT = re.compile(r"^[A-Z][A-Z0-9_$]*$")


class CatalogoIncompletoError(Exception):
    """O catálogo lido não tem tabela que a varredura de 28/10 provou existir."""


def so_select(sql: str) -> str:
    # `raise`, não `assert`: com `python -O` o assert some e a guarda junto.
    if not _SO_SELECT.match(sql):
        raise RuntimeError("só SELECT é permitido")
    return sql


def _ident(nome: str) -> str:
    if not _IDENT.fullmatch(nome):
        raise ValueError(f"identificador inválido: {nome!r}")
    return f'"{nome}"'


def colunas_de_uso(catalogo: list[tuple[str, str, int]]) -> list[tuple[str, str]]:
    """(tabela, coluna) do catálogo (tabela, coluna, tipo) que pode guardar um SEQ de produto:
    inteira, com PROD (ou material, tecido, fio e variante: `_EXTRA_NOME`) no nome, em tabela de
    usuário, fora de `_FORA`."""
    return sorted(
        {
            (t, c)
            for t, c, tipo in catalogo
            if tipo in TIPOS_INTEIROS
            and ("PROD" in c or _EXTRA_NOME.search(c))
            and not t.startswith(PREFIXOS_SISTEMA)
            and (t, c) not in _FORA
        }
    )


def exige_tabelas_obrigatorias(alvos: list[tuple[str, str]]) -> None:
    faltam = TABELAS_OBRIGATORIAS - {t for t, _ in alvos}
    if faltam:
        raise CatalogoIncompletoError("catálogo sem " + ", ".join(sorted(faltam)))


def sql_de_uso(tabela: str, coluna: str) -> str:
    return so_select(f"SELECT FIRST 1 1 FROM {_ident(tabela)} WHERE {_ident(coluna)} = ?")


def alvos_do_fire(conn) -> list[tuple[str, str]]:
    """Lê o catálogo pela conexão e devolve as colunas de uso, ou levanta
    `CatalogoIncompletoError`. Uma leitura por chamada: quem chama guarda o resultado."""
    cur = conn.cursor()
    try:
        cur.execute(so_select(SQL_CATALOGO))
        alvos = colunas_de_uso([(r[0], r[1], r[2]) for r in cur.fetchall()])
    finally:
        cur.close()
    exige_tabelas_obrigatorias(alvos)
    return alvos


def primeiro_uso(conn, seq: int, alvos: list[tuple[str, str]]) -> str | None:
    """`TABELA.COLUNA` da primeira coluna que guarda `seq`, ou None."""
    for tabela, coluna in alvos:
        cur = conn.cursor()
        try:
            cur.execute(sql_de_uso(tabela, coluna), (int(seq),))
            if cur.fetchone() is not None:
                return f"{tabela}.{coluna}"
        finally:
            cur.close()
    return None
