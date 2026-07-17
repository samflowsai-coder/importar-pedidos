"""Cópia local dos clientes ativos do Fire (`clientes_fire`, db do ambiente).

Snapshot substitutivo (delete + insert); o envio ao Flow é decisão separada
(flowpcp_clientes_push). Recebe a conexão aberta (mesmo padrão do catalogo_fire_repo).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

    from app.erp.cliente_extract import ClienteFireDTO

_COLS = ("fire_cliente_id", "cnpj", "nome", "grupo_codigo", "ativo", "extraido_em")


def replace_all(conn: sqlite3.Connection, dtos: list[ClienteFireDTO], *, extraido_em: str) -> int:
    """Substitui o snapshot inteiro pela extração atual. Retorna o total gravado."""
    conn.execute("DELETE FROM clientes_fire")
    conn.executemany(
        f"INSERT INTO clientes_fire ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
        [
            (d.fire_cliente_id, d.cnpj, d.nome, d.grupo_codigo, 1 if d.ativo else 0, extraido_em)
            for d in dtos
        ],
    )
    return len(dtos)


def list_all(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM clientes_fire ORDER BY fire_cliente_id"
    ).fetchall()
    return [dict(zip(_COLS, r, strict=True)) for r in rows]


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM clientes_fire").fetchone()[0]
