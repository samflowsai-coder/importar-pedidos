"""Mover um pedido em revisão para a outra empresa (spec regra-faturamento §6.5, §10.4).

Mover = a linha de `imports` nasce no `app_state_<destino>.db` com o MESMO id
(o rastro não se parte) e some do `app_state_<origem>.db`. O arquivo original
não se mexe (`original_path` continua valendo). Nada derivado do Fire ou da
empresa de origem viaja: check de produto, check de preço, escolha pontual
(SEQ do Fire de origem), cliente escolhido, ack de sem preço — o destino
recalcula (quem chama, ver a rota `mover-empresa` em `app/web/server.py`).

Só move pedido que não saiu do Portal: em revisão, sem número do Fire, sem
XLSX, sem nada no outbox (Flow/Gestor) e sem revisão de preço que o Flow possa
conhecer. Rastro de saída é motivo de `PedidoNaoMovivel`, nunca de apagar.

Dois bancos não fecham numa transação só. A origem fica travada (BEGIN
IMMEDIATE) do começo ao fim: a guarda vale até o DELETE, e nada novo entra
nela no meio. Ordem: copiar no destino (commit) e só então apagar na origem.
Interrompido no meio, o pedido fica nos dois e a próxima chamada conclui — a
cópia é idempotente (INSERT OR IGNORE; audit e eventos sem repetir). Se na
janela a origem saiu do Portal, a cópia do destino é desfeita: nunca dois.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter
from datetime import datetime
from typing import Any

from app.persistence import router
from app.utils.logger import logger

# Um mover por vez no processo: dois cliques no mesmo pedido não correm um
# contra o outro (o segundo encontra o pedido já movido e não faz nada).
_lock = threading.Lock()

# Colunas de `imports` derivadas do Fire/empresa de origem: nascem NULL no destino.
_ZERADAS_NO_DESTINO = (
    "check_json",
    "check_preco_json",
    "produto_escolhido_json",  # R18: fire_seq do Fire de origem
    "db_result_json",
    "cliente_override_codigo",
    "cliente_override_razao",
    "cliente_override_at",
    "cliente_override_by",
    "cliente_override_cnpj",
    "sem_preco_ack_by",
    "sem_preco_ack_at",
    "sem_preco_ack_items",
    "sent_to_fire_at",
    "fire_status_last_seen",
    "fire_status_polled_at",
    "reconciled_at",
)

# Tabelas do banco da empresa com `import_id`. A guarda garante que as de
# rastro de saída estão vazias (ou só com revisão de preço que nunca saiu);
# apagar todas é o cinto de segurança contra o ON DELETE CASCADE desligado.
_APAGADAS_NA_ORIGEM = (
    "erp_item_vinculo",
    "erp_xlsx_linha",
    "preco_revisao",
    "preco_revisao_piso",
    "preco_decisao",
    "outbox",
    "audit_log",
    "order_lifecycle_events",
)

_AUDIT_COLS = ("event_type", "detail_json", "created_at")
_EVENTO_COLS = ("event_type", "source", "payload_json", "trace_id", "occurred_at", "ingested_at")


class PedidoNaoMovivel(Exception):  # noqa: N818 — nome do contrato do plano
    """O pedido não pode mudar de empresa; `motivo` vai para o operador."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


def _tem(conn: sqlite3.Connection, tabela: str, import_id: str, extra: str = "") -> bool:
    return (
        conn.execute(
            f"SELECT 1 FROM {tabela} WHERE import_id = ?{extra} LIMIT 1",  # noqa: S608 — constante
            (import_id,),
        ).fetchone()
        is not None
    )


def _motivo(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """Por que este pedido não se move, ou None. Lê só o banco de `conn`."""
    iid = row["id"]
    if row["fire_codigo"] is not None or _tem(conn, "erp_item_vinculo", iid):
        return "já foi para o Fire"
    if (row["output_files_json"] or "").strip() not in ("", "[]") or _tem(
        conn, "erp_xlsx_linha", iid
    ):
        return "já foi exportado em XLSX"
    if row["portal_status"] != "parsed":
        return "não está em revisão"
    if row["gestor_order_id"] or row["apontae_order_id"] or _tem(conn, "outbox", iid):
        return "já foi enviado ao Flow ou ao Gestor"
    if (
        _tem(conn, "preco_revisao", iid, " AND (enviada_em IS NOT NULL OR tentada_em IS NOT NULL)")
        or _tem(conn, "preco_revisao_piso", iid)
        or _tem(conn, "preco_decisao", iid)
    ):
        return "o preço já foi para aprovação no Flow"
    return None


def _copiar_faltantes(
    dconn: sqlite3.Connection,
    tabela: str,
    cols: tuple[str, ...],
    linhas: list[sqlite3.Row],
    import_id: str,
    env_id: str,
) -> None:
    """Copia as linhas da origem que o destino ainda não tem (multiconjunto por
    conteúdo): na 1ª vez copia tudo; ao concluir um mover interrompido, só o
    que entrou na origem depois — nunca duplica."""
    lista = ", ".join(cols)
    ja = Counter(
        tuple(r)
        for r in dconn.execute(
            f"SELECT {lista} FROM {tabela} WHERE import_id = ?",  # noqa: S608 — constante
            (import_id,),
        ).fetchall()
    )
    marcas = ", ".join("?" * (len(cols) + 2))
    for r in linhas:
        valores = tuple(r[c] for c in cols)
        if ja[valores] > 0:
            ja[valores] -= 1
            continue
        dconn.execute(
            f"INSERT INTO {tabela} (environment_id, import_id, {lista}) VALUES ({marcas})",  # noqa: S608
            (env_id, import_id, *valores),
        )


def _copiar_no_destino(
    oconn: sqlite3.Connection, row: sqlite3.Row, *, origem: dict, destino: dict, por: str
) -> None:
    iid = row["id"]
    linha = dict(row) | dict.fromkeys(_ZERADAS_NO_DESTINO) | {"environment_id": destino["id"]}
    audit = oconn.execute(
        f"SELECT {', '.join(_AUDIT_COLS)} FROM audit_log WHERE import_id = ? ORDER BY id",  # noqa: S608
        (iid,),
    ).fetchall()
    eventos = oconn.execute(
        f"SELECT {', '.join(_EVENTO_COLS)} FROM order_lifecycle_events"  # noqa: S608
        " WHERE import_id = ? ORDER BY id",
        (iid,),
    ).fetchall()
    with router.env_connect(destino["slug"]) as dconn:
        dconn.execute("BEGIN IMMEDIATE")
        novo = (
            dconn.execute(
                f"INSERT OR IGNORE INTO imports ({', '.join(linha)})"  # noqa: S608
                f" VALUES ({', '.join('?' * len(linha))})",
                tuple(linha.values()),
            ).rowcount
            == 1
        )
        _copiar_faltantes(dconn, "audit_log", _AUDIT_COLS, audit, iid, destino["id"])
        _copiar_faltantes(
            dconn, "order_lifecycle_events", _EVENTO_COLS, eventos, iid, destino["id"]
        )
        if novo:
            # Na mesma transação da linha: uma vez por mover, mesmo se interrompido.
            dconn.execute(
                "INSERT INTO audit_log (environment_id, import_id, event_type, detail_json,"
                " created_at) VALUES (?, ?, 'movido_de_empresa', ?, ?)",
                (
                    destino["id"],
                    iid,
                    json.dumps(
                        {"de": origem["slug"], "para": destino["slug"], "por": por},
                        ensure_ascii=False,
                    ),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )


def _apagar_pedido(conn: sqlite3.Connection, import_id: str) -> None:
    for tabela in _APAGADAS_NA_ORIGEM:
        conn.execute(f"DELETE FROM {tabela} WHERE import_id = ?", (import_id,))  # noqa: S608
    conn.execute("DELETE FROM imports WHERE id = ?", (import_id,))


def _apagar_na_origem(conn: sqlite3.Connection, import_id: str) -> None:
    _apagar_pedido(conn, import_id)


def _desfazer_copia(import_id: str, destino: dict) -> None:
    """A origem não pode mais sair (foi ao Fire na janela de um mover
    interrompido): a cópia do destino, se ainda em revisão, some. Se ela também
    já saiu, fica — os dois lados são de verdade e só um humano resolve."""
    with router.env_connect(destino["slug"]) as dconn:
        dconn.execute("BEGIN IMMEDIATE")
        row = dconn.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
        if row is None:
            return
        if _motivo(dconn, row) is not None:
            logger.error(
                "mover.pedido_nos_dois_bancos import_id={} destino={}", import_id, destino["slug"]
            )
            return
        _apagar_pedido(dconn, import_id)
    logger.warning("mover.copia_desfeita import_id={} destino={}", import_id, destino["slug"])


def mover_import(
    import_id: str, *, origem: dict[str, Any], destino: dict[str, Any], por: str
) -> bool:
    """Move o pedido de `origem` para `destino`. True = moveu agora; False = já
    estava movido (só no destino). `PedidoNaoMovivel` = nada mudou na origem."""
    with _lock, router.env_connect(origem["slug"]) as oconn:
        oconn.execute("BEGIN IMMEDIATE")
        row = oconn.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
        if row is None:
            with router.env_connect(destino["slug"]) as dconn:
                no_destino = dconn.execute(
                    "SELECT 1 FROM imports WHERE id = ?", (import_id,)
                ).fetchone()
            if no_destino is not None:
                return False
            raise PedidoNaoMovivel("não encontrado")
        motivo = _motivo(oconn, row)
        if motivo is not None:
            _desfazer_copia(import_id, destino)
            raise PedidoNaoMovivel(motivo)
        _copiar_no_destino(oconn, row, origem=origem, destino=destino, por=por)
        _apagar_na_origem(oconn, import_id)
    logger.info(
        "mover.pedido import_id={} de={} para={} por={}",
        import_id,
        origem["slug"],
        destino["slug"],
        por,
    )
    return True
