"""Única porta de ESCRITA do cadastro de produto no Fire (volta Flow -> Fire).

Uma transação por cadastro, SQL sempre parametrizado, nunca DELETE em PRODUTOS
(só a composição do pai em PRODUTOS_KIT é trocada). `PRODUTOS.SEQ` e
`PRODUTOS_KIT.CODIGO` não têm gerador: MAX+1 sob lock, e colisão com a tela do
Fire tenta de novo em transação nova (spec 2026-10-16 §6.3).

`PADROES_P0` são as colunas que a tela do Fire grava num cadastro novo e que o
Flow não manda. Fica vazio até a P0 (diff numa cópia do .fdb); até lá os modos
de gravação só rodam em ambiente liberado por `PORTAL_FLOWPCP_CADASTRO_P0`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.erp.exceptions import FirebirdConnectionError

COLUNAS_PERMITIDAS = frozenset(
    {"DESCRICAO", "UNIDADE", "CODIGO_EAN13", "CLASSIF_FISCAL", "CODITEMSPED",
     "CODPARANFE", "CODPROD_ALTERN", "GRUPO", "MARCA"}
)
PADROES_P0: dict[str, object] = {}
TENTATIVAS_SEQ = 3
_lock = threading.Lock()

_TEXTOS_LOCK = ("deadlock", "lock conflict", "update conflicts")
_TEXTOS_CONEXAO = (
    "network",
    "connection",
    "error writing data to the connection",
    "unable to complete network request",
)


@dataclass(frozen=True)
class Gravacao:
    seq: int
    acao: str


class SeqTomadoError(Exception):
    """MAX+1 colidiu TENTATIVAS_SEQ vezes seguidas."""


def _checa_colunas(campos: dict) -> None:
    fora = set(campos) - COLUNAS_PERMITIDAS
    if fora:
        raise ValueError(f"coluna fora da lista do gravador: {sorted(fora)}")


def _um(cur, sql: str, params: tuple = ()):
    cur.execute(sql, params)
    linha = cur.fetchone()
    return linha[0] if linha else None


def classificar_erro(exc: BaseException) -> Literal["colisao", "lock", "conexao", "recusa"]:
    """Classe de uma falha do Firebird, pelo tipo e pelo texto. O resto é recusa
    (NOT NULL, truncamento, conversão): não se tenta de novo."""
    if isinstance(exc, FirebirdConnectionError):
        return "conexao"
    texto = str(exc).lower()
    if "violation" in texto or "primary" in texto or "unique" in texto:
        return "colisao"
    if any(t in texto for t in _TEXTOS_LOCK):
        return "lock"
    if any(t in texto for t in _TEXTOS_CONEXAO):
        return "conexao"
    return "recusa"


def _colisao(exc: Exception) -> bool:
    return classificar_erro(exc) == "colisao"


def _insere_composicao(cur, pai: int, componentes: list[tuple[int, float]]) -> None:
    for filho, qtd in componentes:
        codigo = int(_um(cur, "SELECT MAX(CODIGO) FROM PRODUTOS_KIT") or 0) + 1
        cur.execute(
            "INSERT INTO PRODUTOS_KIT (CODIGO, CODPRODUTO_PAI, CODPRODUTO, QTD) VALUES (?, ?, ?, ?)",
            (codigo, pai, filho, qtd),
        )


def criar(conn, *, campos, componentes, hoje: date, ao_reservar: Callable[[int], None]) -> Gravacao:
    _checa_colunas(campos)
    linha = {**PADROES_P0, **campos}
    with _lock:
        for _ in range(TENTATIVAS_SEQ):
            cur = conn.cursor()
            try:
                seq = int(_um(cur, "SELECT MAX(SEQ) FROM PRODUTOS") or 0) + 1
                ao_reservar(seq)
                colunas = ["SEQ", "BLOQUEADO", "DATA_CAD", *linha]
                valores = (seq, "Nao", hoje, *linha.values())
                cur.execute(
                    f"INSERT INTO PRODUTOS ({', '.join(colunas)}) "
                    f"VALUES ({', '.join('?' for _ in colunas)})",
                    valores,
                )
                if componentes:
                    _insere_composicao(cur, seq, componentes)
                conn.commit()
                return Gravacao(seq=seq, acao="criar")
            except Exception as exc:
                conn.rollback()
                if not _colisao(exc):
                    raise
            finally:
                cur.close()
    raise SeqTomadoError(f"SEQ tomado {TENTATIVAS_SEQ} vezes seguidas")


def atualizar(conn, *, seq: int, campos, componentes) -> Gravacao:
    _checa_colunas(campos)
    cur = conn.cursor()
    try:
        if campos:
            sets = ", ".join(f"{c} = ?" for c in campos)
            cur.execute(f"UPDATE PRODUTOS SET {sets} WHERE SEQ = ?", (*campos.values(), seq))
            if cur.rowcount == 0:
                raise LookupError(f"SEQ {seq} não existe no Fire")
        elif _um(cur, "SELECT SEQ FROM PRODUTOS WHERE SEQ = ?", (seq,)) is None:
            raise LookupError(f"SEQ {seq} não existe no Fire")
        if componentes is not None:
            cur.execute("DELETE FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ?", (seq,))
            _insere_composicao(cur, seq, componentes)
        conn.commit()
        return Gravacao(seq=seq, acao="atualizar")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def inativar(conn, *, seq: int) -> Gravacao:
    cur = conn.cursor()
    try:
        cur.execute("UPDATE PRODUTOS SET BLOQUEADO = 'Sim' WHERE SEQ = ?", (seq,))
        if cur.rowcount == 0:
            raise LookupError(f"SEQ {seq} não existe no Fire")
        conn.commit()
        return Gravacao(seq=seq, acao="inativar")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


_CONFERENCIA = ("SEQ", "DESCRICAO", "UNIDADE", "GRUPO", "CODIGO_EAN13", "CLASSIF_FISCAL", "DATA_CAD")


def ler_produto(conn, seq: int) -> dict | None:
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT {', '.join(_CONFERENCIA)} FROM PRODUTOS WHERE SEQ = ?", (seq,))
        linha = cur.fetchone()
        return dict(zip(_CONFERENCIA, linha, strict=True)) if linha else None
    finally:
        cur.close()


def ler_composicao(conn, seq: int) -> list[tuple[int, float]]:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT CODPRODUTO, QTD FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ? ORDER BY CODPRODUTO",
            (seq,),
        )
        return [(int(f), float(q)) for f, q in cur.fetchall()]
    finally:
        cur.close()


def buscar_por_nome(conn, *, nome: str, grupo: int, unidade: str, data_cad: date) -> list[int]:
    """Candidatos exatos para a recuperação sem `seq_reservado`. O filtro de nome
    é em Python (o UPPER do Firebird não é confiável em WIN1252)."""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT SEQ, DESCRICAO FROM PRODUTOS WHERE GRUPO = ? AND UNIDADE = ? AND DATA_CAD = ?",
            (grupo, unidade, data_cad),
        )
        alvo = nome.strip().casefold()
        return [int(s) for s, d in cur.fetchall() if str(d or "").strip().casefold() == alvo]
    finally:
        cur.close()
