"""Rastro das decisões de empresa do roteamento (shared). Só INSERT."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.persistence import router

_COLS = (
    "id", "import_id", "decidido_em", "modo", "degrau", "env_sugerido",
    "env_escolhido", "decidido_por", "motivo", "acao",
)


def registrar(
    *,
    import_id: str,
    modo: str,
    degrau: str | None,
    env_sugerido: str | None,
    env_escolhido: str,
    decidido_por: str,
    motivo: str | None,
    acao: str,
) -> None:
    """Grava uma decisão. Nunca levanta: rastro não pode derrubar o import."""
    try:
        with router.shared_connect() as conn:
            conn.execute(
                """INSERT INTO roteamento_decisao
                   (import_id, decidido_em, modo, degrau, env_sugerido,
                    env_escolhido, decidido_por, motivo, acao)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    import_id,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    modo, degrau, env_sugerido, env_escolhido,
                    decidido_por, motivo, acao,
                ),
            )
    except Exception:  # noqa: BLE001 — rastro não pode derrubar o import
        from app.utils.logger import logger

        logger.warning("roteamento.decisao_falhou import_id={}", import_id)


def listar_por_import(import_id: str) -> list[dict[str, Any]]:
    with router.shared_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM roteamento_decisao WHERE import_id = ? ORDER BY id",
            (import_id,),
        ).fetchall()
    return [{k: r[k] for k in _COLS} for r in rows]


def ultima(import_id: str) -> dict[str, Any] | None:
    with router.shared_connect() as conn:
        r = conn.execute(
            "SELECT * FROM roteamento_decisao WHERE import_id = ? ORDER BY id DESC LIMIT 1",
            (import_id,),
        ).fetchone()
    return {k: r[k] for k in _COLS} if r else None
