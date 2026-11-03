"""Cópia local do catálogo do Fire (`catalogo_fire`, db do ambiente).

O sync de catálogo SEMPRE grava a extração aqui (snapshot substitutivo:
delete + insert); o envio ao Flow é decisão separada (flowpcp_catalogo_push).
Recebe a conexão aberta (mesmo padrão de uso do flowpcp_repo nos jobs).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

    from app.erp.catalog_extract import ProdutoFireDTO

_COLS = ("fire_produto_id", "codigo", "nome", "unidade", "ean", "ativo", "tipo", "codprod_altern", "extraido_em")


def replace_all(
    conn: sqlite3.Connection, dtos: list[ProdutoFireDTO], *, extraido_em: str
) -> int:
    """Substitui o snapshot inteiro pela extração atual. Retorna o total gravado."""
    conn.execute("DELETE FROM catalogo_fire")
    conn.executemany(
        f"INSERT INTO catalogo_fire ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
        [
            (d.fire_produto_id, d.codigo, d.nome, d.unidade, d.ean,
             1 if d.ativo else 0, d.tipo, d.codigo_alternativo, extraido_em)
            for d in dtos
        ],
    )
    return len(dtos)


def list_all(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM catalogo_fire ORDER BY fire_produto_id"
    ).fetchall()
    return [dict(zip(_COLS, r, strict=True)) for r in rows]


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM catalogo_fire").fetchone()[0]


def codprod_por_seq(conn: sqlite3.Connection, seqs: list[Any]) -> dict[str, str]:
    """{SEQ em texto: CODPROD_ALTERN} do catálogo local, só dos SEQ que têm código.
    SEQ ausente ou com código vazio fica fora do mapa."""
    chaves = sorted({str(s) for s in seqs})
    out: dict[str, str] = {}
    for i in range(0, len(chaves), 500):
        lote = chaves[i : i + 500]
        rows = conn.execute(
            "SELECT fire_produto_id, codprod_altern FROM catalogo_fire "
            f"WHERE fire_produto_id IN ({', '.join('?' * len(lote))})",
            lote,
        ).fetchall()
        for seq, codigo in rows:
            if codigo and str(codigo).strip():
                out[str(seq)] = str(codigo).strip()
    return out
