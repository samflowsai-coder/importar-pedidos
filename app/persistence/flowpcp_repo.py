from __future__ import annotations

import sqlite3


def get_last_cursor(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT last_cursor FROM flowpcp_cursor_state WHERE id = 1").fetchone()
    return row["last_cursor"] if row else None


def save_last_cursor(conn: sqlite3.Connection, cursor: str) -> None:
    conn.execute(
        """
        INSERT INTO flowpcp_cursor_state (id, last_cursor, atualizado_em)
        VALUES (1, ?, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET last_cursor = excluded.last_cursor,
                                      atualizado_em = datetime('now')
        """,
        (cursor,),
    )
    conn.commit()


def get_attempts_count(conn: sqlite3.Connection, decisao_id: str) -> int:
    row = conn.execute(
        "SELECT attempts FROM flowpcp_decisoes_mapping WHERE decisao_id = ?", (decisao_id,)
    ).fetchone()
    return int(row["attempts"]) if row else 0


def register_attempt(conn: sqlite3.Connection, decisao_id: str) -> int:
    conn.execute(
        """
        INSERT INTO flowpcp_decisoes_mapping (decisao_id, attempts)
        VALUES (?, 1)
        ON CONFLICT(decisao_id) DO UPDATE SET attempts = attempts + 1,
                                              atualizado_em = datetime('now')
        """,
        (decisao_id,),
    )
    conn.commit()
    return get_attempts_count(conn, decisao_id)


def mark_reconciliada(conn: sqlite3.Connection, decisao_id: str, acao: str) -> None:
    conn.execute(
        """
        INSERT INTO flowpcp_decisoes_mapping (decisao_id, acao_executada, reconciliado_em, atualizado_em)
        VALUES (?, ?, datetime('now'), datetime('now'))
        ON CONFLICT(decisao_id) DO UPDATE SET acao_executada = excluded.acao_executada,
                                              reconciliado_em = datetime('now'),
                                              atualizado_em = datetime('now')
        """,
        (decisao_id, acao),
    )
    conn.commit()
