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

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from loguru import logger

from app.erp.exceptions import FirebirdConnectionError

COLUNAS_PERMITIDAS = frozenset(
    {"DESCRICAO", "UNIDADE", "CODIGO_EAN13", "CLASSIF_FISCAL", "CODITEMSPED",
     "CODPARANFE", "CODPROD_ALTERN", "GRUPO", "MARCA"}
)
PADROES_P0: dict[str, object] = {}
TENTATIVAS_SEQ = 3
_lock = threading.Lock()

# Fallback por texto, só com a frase inteira: o Firebird repete na mensagem o
# valor recebido, então palavra solta ("unique", "connection") engana.
_TEXTOS_COLISAO = (
    "violation of primary or unique key constraint",
    "attempt to store duplicate value",
)
_TEXTOS_CONEXAO = (
    "unable to complete network request",
    "error writing data to the connection",
    "error reading data from the connection",
    "connection lost",
)
_TEXTOS_LOCK = ("deadlock", "lock conflict", "update conflicts with concurrent update")

# gdscode do Firebird (iberror.h, conferido contra o iberror_c.h do Firebird 5.0).
_GDS = {
    335544665: "colisao",  # isc_unique_key_violation
    335544349: "colisao",  # isc_no_dup
    335544336: "lock",  # isc_deadlock
    335544345: "lock",  # isc_lock_conflict
    335544451: "lock",  # isc_update_conflict
    335544510: "lock",  # isc_lock_timeout
    335544721: "conexao",  # isc_network_error
    335544726: "conexao",  # isc_net_read_err
    335544727: "conexao",  # isc_net_write_err
    335544741: "conexao",  # isc_lost_db_connection
    335544466: "recusa",  # isc_foreign_key
}
# -902 fica de fora: balde genérico (login inválido e I/O também caem nele).
_SQLCODE = {-803: "colisao", -913: "lock", -530: "recusa"}

# Colunas que o gravador monta sozinho ou que não são do Flow: nunca em PADROES_P0.
_RESERVADAS = frozenset({"SEQ", "BLOQUEADO", "DATA_CAD"})


@dataclass(frozen=True)
class Gravacao:
    seq: int
    acao: str


class SeqTomadoError(Exception):
    """MAX+1 colidiu TENTATIVAS_SEQ vezes seguidas."""


class ReservaFalhouError(Exception):
    """`ao_reservar` (livro SQLite) falhou: não é erro do Fire e não se repete."""


def _checa_colunas(campos: dict) -> None:
    fora = set(campos) - COLUNAS_PERMITIDAS
    if fora:
        raise ValueError(f"coluna fora da lista do gravador: {sorted(fora)}")


def _valida_padroes(padroes: dict) -> None:
    for chave in padroes:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", chave):
            raise ValueError(f"PADROES_P0: nome de coluna inválido: {chave!r}")
    cruzam = set(padroes) & (_RESERVADAS | COLUNAS_PERMITIDAS)
    if cruzam:
        raise ValueError(f"PADROES_P0 cruza colunas reservadas/permitidas: {sorted(cruzam)}")


_valida_padroes(PADROES_P0)


def _um(cur, sql: str, params: tuple = ()):
    cur.execute(sql, params)
    linha = cur.fetchone()
    return linha[0] if linha else None


def classificar_erro(exc: BaseException) -> Literal["colisao", "lock", "conexao", "recusa"]:
    """Classe de uma falha do Firebird. O resto é recusa (NOT NULL, truncamento,
    conversão, FK): não se tenta de novo.

    Ordem: tipo, depois os números que o fdb monta em `args` (msg, sqlcode,
    gdscode), por último o texto (frase inteira, conexão antes de lock)."""
    if isinstance(exc, FirebirdConnectionError):
        return "conexao"
    args = getattr(exc, "args", ())
    if len(args) == 3 and isinstance(args[1], int) and isinstance(args[2], int):
        if args[2] in _GDS:
            return _GDS[args[2]]
        # Com os números do driver, o texto não decide: ele repete o valor do usuário.
        return _SQLCODE.get(args[1], "recusa")
    texto = str(exc).lower()
    if any(t in texto for t in _TEXTOS_COLISAO):
        return "colisao"
    if any(t in texto for t in _TEXTOS_CONEXAO):
        return "conexao"
    if any(t in texto for t in _TEXTOS_LOCK):
        return "lock"
    return "recusa"


def _colisao(exc: BaseException) -> bool:
    return classificar_erro(exc) == "colisao"


def _desfaz(conn) -> bool:
    """Rollback que nunca mascara a exceção original: se falhar, só loga e
    devolve False (quem chama relança a original, sem nova tentativa)."""
    try:
        conn.rollback()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("cadastro_fire: rollback falhou ({})", type(exc).__name__)
        return False


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
            insercao = False  # só colisão do INSERT (MAX+1) se repete
            try:
                seq = int(_um(cur, "SELECT MAX(SEQ) FROM PRODUTOS") or 0) + 1
                try:
                    ao_reservar(seq)
                except Exception as exc:
                    raise ReservaFalhouError("reserva do SEQ no livro falhou") from exc
                insercao = True
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
            except BaseException as exc:
                if not _desfaz(conn) or not insercao or not _colisao(exc):
                    raise
            finally:
                cur.close()
    raise SeqTomadoError(f"SEQ tomado {TENTATIVAS_SEQ} vezes seguidas")


def _atualizar_uma_vez(conn, seq: int, campos, componentes, estado: dict) -> Gravacao:
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
            estado["composicao"] = True
            cur.execute("DELETE FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ?", (seq,))
            _insere_composicao(cur, seq, componentes)
        conn.commit()
        return Gravacao(seq=seq, acao="atualizar")
    except BaseException:
        estado["rollback_ok"] = _desfaz(conn)
        raise
    finally:
        cur.close()


def atualizar(conn, *, seq: int, campos, componentes) -> Gravacao:
    _checa_colunas(campos)
    if componentes is None:
        return _atualizar_uma_vez(conn, seq, campos, componentes, {})
    # Composição usa MAX(CODIGO)+1: mesmo lock e nova tentativa do criar.
    with _lock:
        for _ in range(TENTATIVAS_SEQ):
            estado: dict = {}
            try:
                return _atualizar_uma_vez(conn, seq, campos, componentes, estado)
            except Exception as exc:
                # só colisão da composição (MAX+1) se repete; a do UPDATE sai como veio
                if not estado.get("rollback_ok") or not estado.get("composicao") or not _colisao(exc):
                    raise
    raise SeqTomadoError(f"CODIGO de composição tomado {TENTATIVAS_SEQ} vezes seguidas")


def inativar(conn, *, seq: int) -> Gravacao:
    cur = conn.cursor()
    try:
        cur.execute("UPDATE PRODUTOS SET BLOQUEADO = 'Sim' WHERE SEQ = ?", (seq,))
        if cur.rowcount == 0:
            raise LookupError(f"SEQ {seq} não existe no Fire")
        conn.commit()
        return Gravacao(seq=seq, acao="inativar")
    except BaseException:
        _desfaz(conn)
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
