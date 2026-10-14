"""Foto noturna por base: controle e páginas congeladas. Recebe `conn`, não dá commit."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Foto:
    foto_id: str
    base: str
    dia: str
    corte_em: str
    total_paginas: int
    situacao: str
    criada_em: str
    motivo: str | None = None


def _foto(r) -> Foto:
    return Foto(
        r["foto_id"],
        r["base"],
        r["dia"],
        r["corte_em"],
        r["total_paginas"],
        r["situacao"],
        r["criada_em"],
        r["motivo"],
    )


def foto_do_dia(conn: sqlite3.Connection, base: str, dia: str) -> Foto | None:
    r = conn.execute("SELECT * FROM erp_foto WHERE base = ? AND dia = ?", (base, dia)).fetchone()
    return _foto(r) if r else None


def em_envio(conn: sqlite3.Connection, base: str) -> Foto | None:
    r = conn.execute(
        "SELECT * FROM erp_foto WHERE base = ? AND situacao = 'enviando' ORDER BY criada_em DESC LIMIT 1",
        (base,),
    ).fetchone()
    return _foto(r) if r else None


def criar(
    conn: sqlite3.Connection,
    *,
    foto_id: str,
    base: str,
    dia: str,
    corte_em: str,
    paginas: list[dict],
    agora: str,
) -> None:
    conn.execute(
        """
        INSERT INTO erp_foto (foto_id, base, dia, corte_em, total_paginas, criada_em)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (foto_id, base, dia, corte_em, len(paginas), agora),
    )
    conn.executemany(
        "INSERT INTO erp_foto_pagina (foto_id, pagina, payload_json) VALUES (?, ?, ?)",
        [
            (foto_id, p["pagina"], json.dumps(p, ensure_ascii=False, separators=(",", ":")))
            for p in paginas
        ],
    )


def registrar_nao_criada(
    conn: sqlite3.Connection,
    *,
    foto_id: str,
    base: str,
    dia: str,
    corte_em: str,
    motivo: str,
    agora: str,
) -> None:
    """Marca a foto do dia como não criada: `foto_do_dia` passa a existir e a foto
    não é tentada de novo a cada ciclo."""
    conn.execute(
        """
        INSERT INTO erp_foto (foto_id, base, dia, corte_em, total_paginas, situacao,
                              criada_em, concluida_em, motivo)
        VALUES (?, ?, ?, ?, 0, 'nao_criada', ?, ?, ?)
        """,
        (foto_id, base, dia, corte_em, agora, agora, motivo[:500]),
    )


def proxima_pagina(conn: sqlite3.Connection, foto_id: str) -> tuple[int, dict] | None:
    r = conn.execute(
        """
        SELECT pagina, payload_json FROM erp_foto_pagina
        WHERE foto_id = ? AND enviada_em IS NULL ORDER BY pagina LIMIT 1
        """,
        (foto_id,),
    ).fetchone()
    return (r["pagina"], json.loads(r["payload_json"])) if r else None


def marcar_pagina_enviada(conn: sqlite3.Connection, foto_id: str, pagina: int, agora: str) -> None:
    conn.execute(
        "UPDATE erp_foto_pagina SET enviada_em = ? WHERE foto_id = ? AND pagina = ?",
        (agora, foto_id, pagina),
    )


def encerrar(conn: sqlite3.Connection, foto_id: str, situacao: str, agora: str) -> None:
    conn.execute(
        "UPDATE erp_foto SET situacao = ?, concluida_em = ? WHERE foto_id = ?",
        (situacao, agora, foto_id),
    )
