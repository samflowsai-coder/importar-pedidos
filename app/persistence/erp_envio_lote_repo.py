"""Registro dos lotes enviados ao Flow (`erp_envio_lote`, banco do ambiente).

Guarda de onde cada lote saiu (janela, foto, Enviar agora), quem pediu e o que o
Flow respondeu. O lote vazio (sinal de vida) também fica aqui.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

ORIGENS = ("janela", "foto", "manual")

_COLUNAS = (
    "lote_id, base, origem, quem, envio_id, enviado_em, eventos, "
    "http, aceitos, duplicados, rejeitados, respondido_em"
)


@dataclass(frozen=True)
class LoteRegistro:
    lote_id: str
    base: str
    origem: str
    quem: str | None
    envio_id: str | None
    enviado_em: str
    eventos: int
    http: int | None
    aceitos: int
    duplicados: int
    rejeitados: int
    respondido_em: str | None


def _registro(r: sqlite3.Row | tuple) -> LoteRegistro:
    return LoteRegistro(*tuple(r))


def abrir(
    conn: sqlite3.Connection,
    *,
    lote_id: str,
    base: str,
    origem: str,
    quem: str | None,
    envio_id: str | None,
    enviado_em: str,
    eventos: int,
) -> None:
    if origem not in ORIGENS:
        raise ValueError(f"origem inválida: {origem!r} (use {ORIGENS})")
    conn.execute(
        "INSERT INTO erp_envio_lote (lote_id, base, origem, quem, envio_id, enviado_em, eventos) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (lote_id, base, origem, quem, envio_id, enviado_em, eventos),
    )


def fechar(
    conn: sqlite3.Connection,
    lote_id: str,
    *,
    http: int | None,
    aceitos: int,
    duplicados: int,
    rejeitados: int,
    respondido_em: str,
) -> None:
    conn.execute(
        "UPDATE erp_envio_lote SET http = ?, aceitos = ?, duplicados = ?, rejeitados = ?, "
        "respondido_em = ? WHERE lote_id = ?",
        (http, aceitos, duplicados, rejeitados, respondido_em, lote_id),
    )


def recentes(conn: sqlite3.Connection, base: str, limite: int = 20) -> list[LoteRegistro]:
    rows = conn.execute(
        f"SELECT {_COLUNAS} FROM erp_envio_lote WHERE base = ? "
        "ORDER BY enviado_em DESC, lote_id DESC LIMIT ?",
        (base, limite),
    ).fetchall()
    return [_registro(r) for r in rows]


def origens(conn: sqlite3.Connection, lote_ids: list[str]) -> dict[str, LoteRegistro]:
    if not lote_ids:
        return {}
    saida: dict[str, LoteRegistro] = {}
    for i in range(0, len(lote_ids), 500):  # abaixo do limite de variáveis do SQLite
        parte = lote_ids[i : i + 500]
        marcas = ",".join("?" * len(parte))
        for r in conn.execute(
            f"SELECT {_COLUNAS} FROM erp_envio_lote WHERE lote_id IN ({marcas})", parte
        ):
            reg = _registro(r)
            saida[reg.lote_id] = reg
    return saida


def apagar_antigos(conn: sqlite3.Connection, antes_de: str) -> int:
    return conn.execute("DELETE FROM erp_envio_lote WHERE enviado_em < ?", (antes_de,)).rowcount
