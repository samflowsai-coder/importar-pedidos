"""Eco: a mudança de DT_ENTREGA que o próprio Portal aplicou por decisão do Flow.

`poll_decisoes` grava, ao aplicar, a base, os CODIGOs que o UPDATE tocou e a
data nova (`registrar_aplicacao`). Quando a vigia vê `data_entrega_alterada`
num desses pedidos para essa data, o evento sai com `correlacaoId` = id da
decisão, e o Flow rotula `eco_flow` em vez de tratar como mudança do ERP.
Correlaciona por (base, CODIGO tocado, data nova) dentro de 7 dias da
aplicação — o eco só rotula (o Flow já confirmou a decisão por
`confirmar-reconciliacao`), então não há marca de "já ecoou": um UPDATE
que tocou N pedidos ecoa nos N.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

from app.erp_eventos.canonico import formatar_instante

JANELA_ECO = timedelta(days=7)


def registrar_aplicacao(
    conn: sqlite3.Connection,
    decisao_id: str,
    *,
    base: str | None,
    erp_codigos: list[int],
    nova_data: date,
    agora: str,
) -> None:
    conn.execute(
        """
        INSERT INTO flowpcp_decisoes_mapping (decisao_id, base, erp_codigo, erp_codigos_json,
                                              nova_data, aplicada_em)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(decisao_id) DO UPDATE SET
            base = excluded.base, erp_codigo = excluded.erp_codigo,
            erp_codigos_json = excluded.erp_codigos_json, nova_data = excluded.nova_data,
            aplicada_em = excluded.aplicada_em, atualizado_em = datetime('now')
        """,
        (
            decisao_id,
            base,
            erp_codigos[0] if len(erp_codigos) == 1 else None,
            json.dumps(sorted(erp_codigos)),
            nova_data.isoformat(),
            agora,
        ),
    )


def buscar_correlacao(
    conn: sqlite3.Connection, base: str, erp_codigo: int, nova_data: date
) -> str | None:
    desde = formatar_instante(datetime.now(UTC) - JANELA_ECO)
    row = conn.execute(
        """
        SELECT decisao_id FROM flowpcp_decisoes_mapping
        WHERE base = ? AND nova_data = ? AND aplicada_em >= ?
          AND EXISTS (SELECT 1 FROM json_each(erp_codigos_json) WHERE value = ?)
        ORDER BY aplicada_em DESC LIMIT 1
        """,
        (base, nova_data.isoformat(), desde, erp_codigo),
    ).fetchone()
    return row[0] if row else None
