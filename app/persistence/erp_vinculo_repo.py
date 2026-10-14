"""Vínculo item do ERP ↔ item do pedido no Portal, e as linhas de cada XLSX gerado.

Recebe `conn` e não dá commit (mesma regra de `erp_estado_repo`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class VinculoItem:
    erp_item_codigo: int
    erp_codigo: int
    import_id: str
    indice_original: int
    origem: str
    produto_produtor: int | None
    produtor_resolvido_em: str | None


def gravar_vinculos(
    conn: sqlite3.Connection,
    *,
    base: str,
    erp_codigo: int,
    import_id: str,
    pares: list[tuple[int, int]],
    origem: str,
    agora: str,
) -> None:
    """`pares` = [(erp_item_codigo, indice_original)]. Item já vinculado não é sobrescrito."""
    conn.executemany(
        """
        INSERT INTO erp_item_vinculo (base, erp_item_codigo, erp_codigo, import_id,
                                      indice_original, origem, criado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(base, erp_item_codigo) DO NOTHING
        """,
        [(base, item, erp_codigo, import_id, indice, origem, agora) for item, indice in pares],
    )


def vinculos_por_item(
    conn: sqlite3.Connection, base: str, itens: list[int]
) -> dict[int, VinculoItem]:
    if not itens:
        return {}
    marcas = ", ".join("?" for _ in itens)
    rows = conn.execute(
        f"""
        SELECT erp_item_codigo, erp_codigo, import_id, indice_original, origem,
               produto_produtor, produtor_resolvido_em
        FROM erp_item_vinculo WHERE base = ? AND erp_item_codigo IN ({marcas})
        """,
        [base, *itens],
    ).fetchall()
    return {r["erp_item_codigo"]: VinculoItem(*r) for r in rows}


def gravar_produtor(
    conn: sqlite3.Connection, base: str, resolvidos: dict[int, int | None], agora: str
) -> None:
    conn.executemany(
        """
        UPDATE erp_item_vinculo SET produto_produtor = ?, produtor_resolvido_em = ?
        WHERE base = ? AND erp_item_codigo = ?
        """,
        [(prod, agora, base, item) for item, prod in resolvidos.items()],
    )


@dataclass(frozen=True)
class LinhaXlsx:
    import_id: str
    arquivo: str
    linha: int
    indice_original: int
    pedido_cliente: str | None
    cnpj_cliente: str | None
    quantidade: str | None
    preco_unitario: str | None


def gravar_linhas_xlsx(
    conn: sqlite3.Connection, import_id: str, linhas: list[LinhaXlsx], agora: str
) -> None:
    """Regerar o XLSX substitui o registro anterior do mesmo import."""
    conn.execute("DELETE FROM erp_xlsx_linha WHERE import_id = ?", (import_id,))
    conn.executemany(
        """
        INSERT INTO erp_xlsx_linha (import_id, arquivo, linha, indice_original, pedido_cliente,
                                    cnpj_cliente, quantidade, preco_unitario, gerado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                lx.import_id,
                lx.arquivo,
                lx.linha,
                lx.indice_original,
                lx.pedido_cliente,
                lx.cnpj_cliente,
                lx.quantidade,
                lx.preco_unitario,
                agora,
            )
            for lx in linhas
        ],
    )


def linhas_xlsx_desde(conn: sqlite3.Connection, desde: str) -> list[LinhaXlsx]:
    """Linhas de XLSX gerados a partir de `desde`, em ordem de import/arquivo/linha."""
    rows = conn.execute(
        """
        SELECT import_id, arquivo, linha, indice_original, pedido_cliente, cnpj_cliente,
               quantidade, preco_unitario
        FROM erp_xlsx_linha WHERE gerado_em >= ?
        ORDER BY import_id, arquivo, linha
        """,
        (desde,),
    ).fetchall()
    return [LinhaXlsx(*r) for r in rows]
