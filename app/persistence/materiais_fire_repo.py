"""Cópia local dos materiais do Fire (`materiais_fire`, db do ambiente).

Snapshot substitutivo (delete + insert), sempre gravado pelo sync de materiais;
o envio ao Flow é decisão separada (interruptor PORTAL_FLOWPCP_MATERIAIS).
Recebe a conexão aberta, como `catalogo_fire_repo`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

_COLS = (
    "erp_produto_id",
    "nome",
    "unidade",
    "ean",
    "ativo",
    "grupo_codigo",
    "ncm",
    "tipo_item_sped",
    "exige_aprovacao",
)


def replace_all(conn: sqlite3.Connection, linhas: list[dict[str, Any]], *, extraido_em: str) -> int:
    """Substitui o snapshot inteiro. Cada dict traz as chaves de `_COLS`."""
    conn.execute("DELETE FROM materiais_fire")
    conn.executemany(
        f"INSERT INTO materiais_fire ({', '.join(_COLS)}, extraido_em) "
        f"VALUES ({', '.join('?' * (len(_COLS) + 1))})",
        [tuple(linha[c] for c in _COLS) + (extraido_em,) for linha in linhas],
    )
    return len(linhas)


def seqs(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT erp_produto_id FROM materiais_fire").fetchall()}
