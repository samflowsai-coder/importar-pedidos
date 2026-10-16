"""Livro da volta Flow → Fire (`cadastro_volta`, db do ambiente).

(cadastro_id, versao) é a chave: registrar duas vezes o mesmo item não duplica.
Recebe a conexão aberta, como `catalogo_fire_repo`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

_COLS = (
    "cadastro_id",
    "versao",
    "estado",
    "acao",
    "tipo",
    "seq_reservado",
    "erro_codigo",
    "erro_mensagem",
    "plano_json",
    "iniciado_em",
    "atualizado_em",
)


def registrar(
    conn: sqlite3.Connection,
    *,
    cadastro_id: str,
    versao: int,
    estado: str,
    acao: str | None,
    tipo: str | None,
    erro_codigo: str | None,
    erro_mensagem: str | None,
    plano_json: str | None,
) -> bool:
    """False se `(cadastro_id, versao)` já está no livro (nada é alterado)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO cadastro_volta "
        "(cadastro_id, versao, estado, acao, tipo, erro_codigo, erro_mensagem, plano_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (cadastro_id, versao, estado, acao, tipo, erro_codigo, erro_mensagem, plano_json),
    )
    return cur.rowcount == 1


def existe(conn: sqlite3.Connection, cadastro_id: str, versao: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM cadastro_volta WHERE cadastro_id = ? AND versao = ?",
        (cadastro_id, versao),
    ).fetchone()
    return row is not None


def listar(conn: sqlite3.Connection, limite: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM cadastro_volta "
        "ORDER BY iniciado_em DESC, rowid DESC LIMIT ?",
        (limite,),
    ).fetchall()
    return [dict(zip(_COLS, r, strict=True)) for r in rows]


def contar_por_estado(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT estado, COUNT(*) FROM cadastro_volta GROUP BY estado").fetchall()
    return {r[0]: r[1] for r in rows}
