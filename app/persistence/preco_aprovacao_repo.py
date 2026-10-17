"""Aprovações de preço por linha de pedido (`preco_aprovacao`, db do ambiente).

A decisão nasce no Flow (Task 7 grava aqui); o envio ao Fire lê. Cada linha é
atada à assinatura do item (produto|preço|quantidade): editou o item, a
decisão antiga deixa de valer.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from app.erp.product_check import _to_cents

if TYPE_CHECKING:
    import sqlite3

    from app.erp.preco_avaliacao import ItemAvaliado
    from app.models.order import OrderItem

Decisao = Literal["acordo", "regra", "preco_inicial", "recusado", "auto_tabela"]
MotivoCancelamento = Literal["pedido_cancelado", "substituido"]


def assinatura(item: OrderItem, fire_produto_id: str | None) -> str:
    cents = _to_cents(item.unit_price)
    qtd = f"{float(item.quantity):g}" if item.quantity is not None else ""
    base = f"{fire_produto_id or ''}|{'' if cents is None else cents}|{qtd}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def registrar_pendencias(
    conn: sqlite3.Connection,
    import_id: str,
    itens: list[tuple[ItemAvaliado, str]],
    *,
    agora: datetime,
) -> list[int]:
    """Cria pendência para (item, assinatura) ainda sem linha; devolve só os ids novos."""
    novos: list[int] = []
    with conn:
        for av, ass in itens:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO preco_aprovacao
                    (import_id, item_index, assinatura, motivo_fila,
                     preco_pedido_centavos, preco_tabela_centavos, criado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    import_id,
                    av.item_index,
                    ass,
                    av.motivo,
                    av.preco_pedido_centavos,
                    av.preco_tabela_centavos,
                    agora.isoformat(),
                ),
            )
            if cur.rowcount:
                novos.append(int(cur.lastrowid))
    return novos


def aplicar_decisao(
    conn: sqlite3.Connection,
    *,
    import_id: str,
    item_index: int,
    assinatura: str,
    decisao: Decisao,
    preco_final_centavos: int | None,
    por: str,
    em: datetime,
    motivo: str | None,
    flow_id: str,
) -> bool:
    """Grava a decisão. False (nada gravado) se não há linha com essa assinatura
    (item mudou) ou se ela foi cancelada. Nova decisão sobrescreve (Flow manda)."""
    with conn:
        cur = conn.execute(
            """
            UPDATE preco_aprovacao
               SET situacao = 'decidida', decisao = ?, preco_final_centavos = ?,
                   decidido_por = ?, decidido_em = ?, motivo_recusa = ?, flow_id = ?
             WHERE import_id = ? AND item_index = ? AND assinatura = ?
               AND cancelada_em IS NULL
            """,
            (
                decisao,
                preco_final_centavos,
                por,
                em.isoformat(),
                motivo,
                flow_id,
                import_id,
                item_index,
                assinatura,
            ),
        )
    return cur.rowcount > 0


def decisoes_validas(
    conn: sqlite3.Connection, import_id: str, assinaturas: dict[int, str]
) -> dict[int, dict[str, Any]]:
    """Decisões não canceladas cuja assinatura é a ATUAL do item, por item_index."""
    out: dict[int, dict[str, Any]] = {}
    rows = conn.execute(
        """
        SELECT * FROM preco_aprovacao
         WHERE import_id = ? AND situacao = 'decidida' AND cancelada_em IS NULL
        """,
        (import_id,),
    ).fetchall()
    for r in rows:
        if assinaturas.get(r["item_index"]) == r["assinatura"]:
            out[r["item_index"]] = dict(r)
    return out


def pendentes_nao_enviadas(conn: sqlite3.Connection, limite: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM preco_aprovacao
         WHERE situacao = 'pendente' AND cancelada_em IS NULL AND enviada_em IS NULL
         ORDER BY id LIMIT ?
        """,
        (limite,),
    ).fetchall()
    return [dict(r) for r in rows]


def _in(ids: list[int]) -> str:
    return ",".join("?" * len(ids))


def marcar_enviada(conn: sqlite3.Connection, ids: list[int], *, em: datetime) -> None:
    if not ids:
        return
    with conn:
        conn.execute(
            f"UPDATE preco_aprovacao SET enviada_em = ? WHERE id IN ({_in(ids)})",  # noqa: S608
            (em.isoformat(), *ids),
        )


def cancelar_do_pedido(
    conn: sqlite3.Connection,
    import_id: str,
    *,
    motivo: MotivoCancelamento,
    em: datetime,
) -> list[int]:
    """Marca como canceladas todas as linhas ainda ativas do pedido; devolve os ids."""
    with conn:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM preco_aprovacao WHERE import_id = ? AND cancelada_em IS NULL "
                "ORDER BY id",
                (import_id,),
            )
        ]
        if ids:
            conn.execute(
                f"UPDATE preco_aprovacao SET cancelada_em = ?, motivo_cancelamento = ? "  # noqa: S608
                f"WHERE id IN ({_in(ids)})",
                (em.isoformat(), motivo, *ids),
            )
    return ids


def cancelamentos_nao_avisados(conn: sqlite3.Connection, limite: int) -> list[dict[str, Any]]:
    """Canceladas que o Flow já conhece (foram enviadas) e ainda não foram avisadas."""
    rows = conn.execute(
        """
        SELECT * FROM preco_aprovacao
         WHERE cancelada_em IS NOT NULL AND enviada_em IS NOT NULL
           AND cancelamento_avisado_em IS NULL
         ORDER BY id LIMIT ?
        """,
        (limite,),
    ).fetchall()
    return [dict(r) for r in rows]


def marcar_cancelamento_avisado(conn: sqlite3.Connection, ids: list[int], *, em: datetime) -> None:
    if not ids:
        return
    with conn:
        conn.execute(
            f"UPDATE preco_aprovacao SET cancelamento_avisado_em = ? "  # noqa: S608
            f"WHERE id IN ({_in(ids)})",
            (em.isoformat(), *ids),
        )
