"""Interruptor do roteamento, tabela sombra e fila de pendências (shared).

O interruptor tem TRÊS estados, não dois, porque o problema do Samuel é de
sequência: ele não pode validar o que não está rodando, e não pode ligar o que
não validou. `observando` é o estado que resolve isso — o roteador roda, grava
o que teria feito, e não age.

Regra que não se relaxa: **instalação nova nasce em `'desligado'`**, e
`'desligado'` significa que o roteador nem é chamado.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.persistence import router

MODOS: tuple[str, ...] = ("desligado", "observando", "ligado")
DESLIGADO, OBSERVANDO, LIGADO = MODOS

_PEND_FIELDS = (
    "sha256",
    "source_path",
    "env_scan_slug",
    "order_number",
    "customer_cnpj",
    "customer_name",
    "visto_em",
    "visto_vezes",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def modo() -> str:
    """Modo atual. Sem linha na tabela = `'desligado'` — o default é o seguro."""
    with router.shared_connect() as conn:
        row = conn.execute("SELECT valor FROM roteamento_modo WHERE id = 1").fetchone()
    return row[0] if row else DESLIGADO


def set_modo(valor: str, *, por: str) -> None:
    """Troca o modo. Recusa valor fora de `MODOS` sem tocar no estado atual."""
    if valor not in MODOS:
        raise ValueError(f"modo inválido: {valor!r} — use um de {MODOS}")
    with router.shared_connect() as conn:
        conn.execute(
            """INSERT INTO roteamento_modo (id, valor, alterado_por, alterado_em)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   valor = excluded.valor,
                   alterado_por = excluded.alterado_por,
                   alterado_em = excluded.alterado_em""",
            (valor, por, _now()),
        )


def registrar_sombra(
    *,
    import_id: str,
    degrau: str,
    env_sugerido: str | None,
    env_escolhido: str | None,
) -> None:
    """Registra a decisão sombra de um pedido. Nunca levanta para o chamador.

    Em `observando` isto roda no caminho do commit. Falhar aqui não pode
    impedir a importação de um pedido — a evidência é importante, o pedido é
    mais. `KeyboardInterrupt` continua propagando: só `Exception` é engolida.

    `bateu` exige `degrau != "perguntar"` além de sugerido == escolhido: um
    degrau 'perguntar' é, por definição, "não tive sugestão" — não existe
    acerto para contar ali, mesmo que o chamador (por engano) mande um
    `env_sugerido` que coincida com o escolhido. É esta função que grava o
    campo, então é aqui que o significado dele é decidido — não é contrato
    para o chamador respeitar sozinho.
    """
    bateu = (
        1
        if (degrau != "perguntar" and env_sugerido is not None and env_sugerido == env_escolhido)
        else 0
    )
    try:
        with router.shared_connect() as conn:
            conn.execute(
                """INSERT INTO roteamento_sombra
                       (import_id, decidido_em, degrau, env_sugerido,
                        env_escolhido_pelo_operador, bateu)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (import_id, _now(), degrau, env_sugerido, env_escolhido, bateu),
            )
    except Exception:  # noqa: BLE001 — ver docstring: evidência não pode derrubar pedido
        from app.utils.logger import logger

        logger.warning("roteamento.sombra_falhou import_id={}", import_id)


def taxa(dias: int = 30) -> dict[str, Any]:
    """Taxa de acerto da janela: total, bateu, divergiu, não soube responder.

    `divergiu` é contado direto (degrau que não é 'perguntar' e não bateu),
    não por subtração de `total`. `registrar_sombra` já garante que `bateu=1`
    nunca acontece com `degrau='perguntar'` (ver docstring de lá), então
    `bateu`, `perguntar` e `divergiu` particionam `total` exatamente —
    `bateu + perguntar + divergiu == total` sempre, por construção. Contar
    direto (em vez de por subtração) é redundância deliberada: mesmo que essa
    garantia se perca no futuro (linha inserida fora de `registrar_sombra`,
    por exemplo), `divergiu` nunca fica negativo.
    """
    desde = (datetime.now(UTC) - timedelta(days=int(dias))).isoformat(timespec="seconds")
    with router.shared_connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*),
                      COALESCE(SUM(bateu), 0),
                      COALESCE(SUM(CASE WHEN degrau = 'perguntar' THEN 1 ELSE 0 END), 0),
                      COALESCE(SUM(CASE WHEN degrau != 'perguntar' AND bateu = 0
                                        THEN 1 ELSE 0 END), 0)
               FROM roteamento_sombra WHERE decidido_em >= ?""",
            (desde,),
        ).fetchone()
    total, bateu, perguntar, divergiu = (int(row[0]), int(row[1]), int(row[2]), int(row[3]))
    return {
        "total": total,
        "bateu": bateu,
        "perguntar": perguntar,
        "divergiu": divergiu,
        "desde": desde,
    }


def registrar_pendencia(
    *,
    sha256: str,
    source_path: str,
    env_scan_slug: str,
    order_number: str | None,
    customer_cnpj: str | None,
    customer_name: str | None,
) -> None:
    """Marca um arquivo que o watcher não soube rotear. Idempotente por sha."""
    with router.shared_connect() as conn:
        conn.execute(
            """INSERT INTO roteamento_pendencia
                   (sha256, source_path, env_scan_slug, order_number,
                    customer_cnpj, customer_name, visto_em, visto_vezes)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(sha256) DO UPDATE SET
                   visto_em = excluded.visto_em,
                   source_path = excluded.source_path,
                   visto_vezes = roteamento_pendencia.visto_vezes + 1""",
            (
                sha256,
                source_path,
                env_scan_slug,
                order_number,
                customer_cnpj,
                customer_name,
                _now(),
            ),
        )


def listar_pendencias(limit: int = 200) -> list[dict[str, Any]]:
    with router.shared_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM roteamento_pendencia ORDER BY visto_em DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [{k: r[k] for k in _PEND_FIELDS} for r in rows]


def contar_pendencias() -> int:
    with router.shared_connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM roteamento_pendencia").fetchone()
    return int(row[0]) if row else 0


def limpar_pendencia(sha256: str) -> None:
    with router.shared_connect() as conn:
        conn.execute("DELETE FROM roteamento_pendencia WHERE sha256 = ?", (sha256,))
