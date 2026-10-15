"""Estado por pedido do ERP, marca d'água do `seq`, fila de saída e saúde do envio.

Toda função recebe a `conn` do banco do ambiente e NUNCA dá commit: quem
chama decide a transação. A vigia grava estado + seq + eventos de um ciclo
inteiro dentro de um único `with router.env_connect(slug) as conn:`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from app.erp_eventos.eventos import EventoFila
from app.erp_eventos.lotes import Pendente
from app.erp_eventos.modelo import PedidoErp, de_estado, para_estado

# ── estado ────────────────────────────────────────────────────────────────


def carregar_estados(conn: sqlite3.Connection, base: str) -> dict[int, tuple[PedidoErp, str]]:
    rows = conn.execute(
        "SELECT erp_codigo, situacao, estado_json FROM erp_pedido_estado WHERE base = ?", (base,)
    ).fetchall()
    return {r["erp_codigo"]: (de_estado(json.loads(r["estado_json"])), r["situacao"]) for r in rows}


def gravar_estado(
    conn: sqlite3.Connection, base: str, pedido: PedidoErp, situacao: str, agora: str
) -> None:
    conn.execute(
        """
        INSERT INTO erp_pedido_estado (base, erp_codigo, situacao, estado_json, visto_em, atualizado_em)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(base, erp_codigo) DO UPDATE SET
            situacao = excluded.situacao,
            estado_json = excluded.estado_json,
            atualizado_em = excluded.atualizado_em
        """,
        (
            base,
            pedido.erp_codigo,
            situacao,
            json.dumps(para_estado(pedido), ensure_ascii=False),
            agora,
            agora,
        ),
    )


# ── seq ───────────────────────────────────────────────────────────────────


def proximo_seq(conn: sqlite3.Connection, base: str, erp_codigo: int) -> int:
    row = conn.execute(
        "SELECT ultimo_seq FROM erp_seq_marca WHERE base = ? AND erp_codigo = ?", (base, erp_codigo)
    ).fetchone()
    return (row["ultimo_seq"] if row else 0) + 1


def avancar_seq(conn: sqlite3.Connection, base: str, erp_codigo: int, ultimo_usado: int) -> None:
    """Grava a nova marca. Recusa voltar: seq nunca é reutilizado."""
    atual = proximo_seq(conn, base, erp_codigo) - 1
    if ultimo_usado <= atual:
        raise ValueError(f"seq não avança: {base}:{erp_codigo} marca={atual} pedido={ultimo_usado}")
    conn.execute(
        """
        INSERT INTO erp_seq_marca (base, erp_codigo, ultimo_seq) VALUES (?, ?, ?)
        ON CONFLICT(base, erp_codigo) DO UPDATE SET ultimo_seq = excluded.ultimo_seq
        """,
        (base, erp_codigo, ultimo_usado),
    )


# ── fila ──────────────────────────────────────────────────────────────────


def enfileirar(conn: sqlite3.Connection, eventos: list[EventoFila], agora: str) -> None:
    """INSERT puro: id repetido é bug (seq consumido duas vezes) e derruba a transação."""
    conn.executemany(
        """
        INSERT INTO erp_eventos_saida (id, base, erp_codigo, seq, tipo, unidade, payload_json,
                                       observado_em, criado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                e.id,
                e.base,
                e.erp_codigo,
                e.seq,
                e.tipo,
                e.unidade,
                json.dumps(e.payload, ensure_ascii=False, separators=(",", ":")),
                e.observado_em,
                agora,
            )
            for e in eventos
        ],
    )


def pendentes(conn: sqlite3.Connection, base: str, limite: int = 1000) -> list[Pendente]:
    rows = conn.execute(
        """
        SELECT ordem, id, unidade, payload_json FROM erp_eventos_saida
        WHERE base = ? AND situacao = 'pendente' ORDER BY ordem LIMIT ?
        """,
        (base, limite),
    ).fetchall()
    return [Pendente(r["ordem"], r["id"], r["unidade"], r["payload_json"]) for r in rows]


def pendentes_ate(conn: sqlite3.Connection, base: str, corte_em: str) -> int:
    """Eventos ainda pendentes observados até `corte_em` (mesmo formato UTC)."""
    return conn.execute(
        "SELECT COUNT(*) FROM erp_eventos_saida WHERE base = ? AND situacao = 'pendente' "
        "AND observado_em <= ?",
        (base, corte_em),
    ).fetchone()[0]


def marcar_enviados(conn: sqlite3.Connection, ids: list[str], lote_id: str, agora: str) -> None:
    conn.executemany(
        "UPDATE erp_eventos_saida SET lote_id = ?, enviado_em = ? WHERE id = ?",
        [(lote_id, agora, i) for i in ids],
    )


def aplicar_resultados(
    conn: sqlite3.Connection, resultados: dict[str, tuple[str, str | None]], agora: str
) -> None:
    conn.executemany(
        """
        UPDATE erp_eventos_saida SET situacao = ?, motivo = ?, respondido_em = ?
        WHERE id = ? AND situacao = 'pendente'
        """,
        [(sit, motivo, agora, i) for i, (sit, motivo) in resultados.items()],
    )


def contar(conn: sqlite3.Connection, base: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT situacao, COUNT(*) AS n FROM erp_eventos_saida WHERE base = ? GROUP BY situacao",
        (base,),
    ).fetchall()
    return {r["situacao"]: r["n"] for r in rows}


def apagar_entregues_antigos(conn: sqlite3.Connection, antes_de: str) -> int:
    """aceito/duplicado saem depois da janela; rejeitado fica (é o rastro do problema)."""
    cur = conn.execute(
        "DELETE FROM erp_eventos_saida WHERE situacao IN ('aceito', 'duplicado') AND respondido_em < ?",
        (antes_de,),
    )
    return cur.rowcount


# ── saúde do envio ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EnvioEstado:
    base: str
    proxima_tentativa_em: str | None
    falhas_seguidas: int
    bloqueio: str | None
    ultimo_http: int | None
    ultimo_erro: str | None
    ultimo_202_em: str | None
    ultima_vigia_em: str | None
    sinal_pendente: bool


def obter_envio(conn: sqlite3.Connection, base: str) -> EnvioEstado:
    r = conn.execute("SELECT * FROM erp_envio_estado WHERE base = ?", (base,)).fetchone()
    if r is None:
        return EnvioEstado(base, None, 0, None, None, None, None, None, False)
    return EnvioEstado(
        base,
        r["proxima_tentativa_em"],
        r["falhas_seguidas"],
        r["bloqueio"],
        r["ultimo_http"],
        r["ultimo_erro"],
        r["ultimo_202_em"],
        r["ultima_vigia_em"],
        bool(r["sinal_pendente"]),
    )


def _atualizar_envio(conn: sqlite3.Connection, base: str, agora: str, **campos) -> None:
    conn.execute(
        "INSERT INTO erp_envio_estado (base, atualizado_em) VALUES (?, ?) ON CONFLICT(base) DO NOTHING",
        (base, agora),
    )
    sets = ", ".join(f"{k} = ?" for k in campos)
    conn.execute(
        f"UPDATE erp_envio_estado SET {sets}, atualizado_em = ? WHERE base = ?",
        [*campos.values(), agora, base],
    )


def registrar_vigia(conn: sqlite3.Connection, base: str, agora: str) -> None:
    _atualizar_envio(conn, base, agora, ultima_vigia_em=agora, sinal_pendente=1)


def pedir_sinal_de_vida(conn: sqlite3.Connection, base: str, agora: str) -> None:
    _atualizar_envio(conn, base, agora, sinal_pendente=1)


def registrar_202(conn: sqlite3.Connection, base: str, agora: str) -> None:
    _atualizar_envio(
        conn,
        base,
        agora,
        ultimo_202_em=agora,
        ultimo_http=202,
        ultimo_erro=None,
        falhas_seguidas=0,
        proxima_tentativa_em=None,
        sinal_pendente=0,
    )


def registrar_espera(
    conn: sqlite3.Connection, base: str, agora: str, *, http: int | None, erro: str, ate: str
) -> None:
    atual = obter_envio(conn, base)
    _atualizar_envio(
        conn,
        base,
        agora,
        ultimo_http=http,
        ultimo_erro=erro[:500],
        falhas_seguidas=atual.falhas_seguidas + 1,
        proxima_tentativa_em=ate,
    )


def bloquear(
    conn: sqlite3.Connection, base: str, agora: str, *, motivo: str, http: int | None, erro: str
) -> None:
    _atualizar_envio(conn, base, agora, bloqueio=motivo, ultimo_http=http, ultimo_erro=erro[:500])


def desbloquear(conn: sqlite3.Connection, base: str, agora: str) -> None:
    _atualizar_envio(conn, base, agora, bloqueio=None, falhas_seguidas=0, proxima_tentativa_em=None)
