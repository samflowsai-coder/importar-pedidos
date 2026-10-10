"""Emissor do evento `lote_fiscal` (spec SP1 §3A.3 e §4.1).

O PRODUTOR do lote é a Fase 2 do roteamento intercompany (consolidador
semanal), que ainda não existe. Este módulo é o contrato que ela vai chamar,
pronto e testado:

    with router.env_connect(slug_da_produtora) as conn:
        reservar_lote(conn, chave_lote=..., base_espelho=..., agora=...)
    # INSERT do pedido consolidado no ERP da produtora (escrita da Fase 2)
    with router.env_connect(slug_da_produtora) as conn:
        registrar_lote_fiscal(conn, tenant=..., chave_lote=..., base_espelho=...,
                              erp_codigo_espelho=<CODIGO do INSERT>, pernas=[...], agora=...)
    # INSERT falhou? cancelar_reserva(conn, chave_lote=...)

Por que a reserva: o `lote_fiscal` tem que entrar na fila ANTES do
`pedido_visto` do consolidado. Entre o INSERT no ERP e o registro aqui, a
vigia poderia ver o pedido novo; enquanto existe reserva da base, a vigia
segura pedido NOVO da base produtora cujo cliente é intermediário fiscal
(`vigia.py`). Reserva com mais de 1 h é considerada abandonada (log) e não
segura mais nada.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.erp_eventos.canonico import formatar_instante, id_evento, numero_json
from app.erp_eventos.contrato import validar_evento
from app.erp_eventos.eventos import EventoFila
from app.persistence import erp_estado_repo
from app.utils.logger import logger

VALIDADE_RESERVA = timedelta(hours=1)


class ReservaAusenteError(RuntimeError):
    pass


class PernaEmOutroLoteError(ValueError):
    """Um item de origem pertence a um único lote."""


@dataclass(frozen=True)
class PernaLote:
    base_origem: str
    erp_codigo_origem: int
    erp_item_codigo_origem: int
    produto_produtor: int
    quantidade: Decimal
    linha_lote: int


def reservar_lote(
    conn: sqlite3.Connection, *, chave_lote: str, base_espelho: str, agora: datetime
) -> None:
    conn.execute(
        "INSERT INTO erp_lote_reserva (chave_lote, base_espelho, reservado_em) VALUES (?, ?, ?)",
        (chave_lote, base_espelho, formatar_instante(agora)),
    )


def cancelar_reserva(conn: sqlite3.Connection, *, chave_lote: str) -> None:
    conn.execute("DELETE FROM erp_lote_reserva WHERE chave_lote = ?", (chave_lote,))


def reserva_ativa(conn: sqlite3.Connection, base: str, agora: datetime) -> bool:
    limite = formatar_instante(agora - VALIDADE_RESERVA)
    rows = conn.execute(
        "SELECT chave_lote, reservado_em FROM erp_lote_reserva WHERE base_espelho = ?", (base,)
    ).fetchall()
    ativa = False
    for r in rows:
        if r["reservado_em"] >= limite:
            ativa = True
        else:
            logger.warning(f"erp_eventos: reserva de lote {r['chave_lote']} abandonada (> 1 h)")
    return ativa


def registrar_lote_fiscal(
    conn: sqlite3.Connection,
    *,
    tenant: str,
    chave_lote: str,
    base_espelho: str,
    erp_codigo_espelho: int,
    pernas: list[PernaLote],
    agora: datetime,
) -> EventoFila:
    reserva = conn.execute(
        "SELECT 1 FROM erp_lote_reserva WHERE chave_lote = ? AND base_espelho = ?",
        (chave_lote, base_espelho),
    ).fetchone()
    if reserva is None:
        raise ReservaAusenteError(
            f"lote {chave_lote} sem reserva: chame reservar_lote antes do INSERT"
        )

    observado_em = formatar_instante(agora)
    payload = {
        "id": id_evento(
            tenant=tenant,
            base=base_espelho,
            tipo="lote_fiscal",
            erp_codigo=erp_codigo_espelho,
            erp_item_codigo=0,
            campo=chave_lote,
            antes=None,
            depois=None,
            observado_em=observado_em,
        ),
        "base": base_espelho,
        "tipo": "lote_fiscal",
        "observadoEm": observado_em,
        "seq": None,
        "importId": None,
        "indiceOriginal": None,
        "correlacaoId": None,
        "chaveLote": chave_lote,
        "erpCodigoEspelho": erp_codigo_espelho,
        "pernas": [
            {
                "baseOrigem": p.base_origem,
                "erpCodigoOrigem": p.erp_codigo_origem,
                "erpItemCodigoOrigem": p.erp_item_codigo_origem,
                "produtoProdutor": p.produto_produtor,
                "quantidade": numero_json(p.quantidade),
                "linhaLote": p.linha_lote,
            }
            for p in pernas
        ],
    }
    validar_evento(payload)

    try:
        conn.executemany(
            """
            INSERT INTO erp_lote_perna (base_origem, erp_item_codigo_origem, erp_codigo_origem,
                                        chave_lote, produto_produtor, quantidade, linha_lote, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    p.base_origem,
                    p.erp_item_codigo_origem,
                    p.erp_codigo_origem,
                    chave_lote,
                    p.produto_produtor,
                    str(p.quantidade),
                    p.linha_lote,
                    observado_em,
                )
                for p in pernas
            ],
        )
    except sqlite3.IntegrityError as exc:
        raise PernaEmOutroLoteError(
            f"lote {chave_lote}: item de origem já está em outro lote"
        ) from exc

    evento = EventoFila(
        id=payload["id"],
        base=base_espelho,
        erp_codigo=erp_codigo_espelho,
        seq=None,
        tipo="lote_fiscal",
        unidade=f"lote:{chave_lote}",
        payload=payload,
        observado_em=observado_em,
    )
    erp_estado_repo.enfileirar(conn, [evento], observado_em)
    cancelar_reserva(conn, chave_lote=chave_lote)
    return evento
