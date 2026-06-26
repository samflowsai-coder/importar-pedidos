from __future__ import annotations

import sqlite3

from app.erp.fire_update import update_dt_entrega
from app.integrations.flowpcp.client import FlowPCPClient
from app.integrations.flowpcp.config import FlowPCPConfig
from app.integrations.flowpcp.schema import (
    AcaoReconciliacao,
    ConfirmarReconciliacaoRequest,
    DecisaoFlowPCP,
)
from app.persistence import flowpcp_repo
from app.utils.logger import logger

_MAX_NAO_ENCONTRADO = 5


def _confirmar(
    client: FlowPCPClient,
    conn,
    decisao_id: str,
    acao: AcaoReconciliacao,
    *,
    fire_id_externo: str | None = None,
    observacoes: str | None = None,
) -> None:
    client.confirmar_reconciliacao(
        decisao_id,
        ConfirmarReconciliacaoRequest(
            acao=acao, fire_id_externo=fire_id_externo, observacoes=observacoes
        ),
    )
    flowpcp_repo.mark_reconciliada(conn, decisao_id, acao.value)


def processar_decisao(
    decisao: DecisaoFlowPCP,
    *,
    client: FlowPCPClient,
    fire_conn,
    conn: sqlite3.Connection,
    config: FlowPCPConfig,
) -> None:
    # 1. Rejeitado → cancelamento manual no Fire; Importador só alerta.
    if decisao.status == "rejeitado":
        logger.warning(
            f"flowpcp decisão {decisao.id} rejeitada — cancelamento manual no Fire "
            f"(pedido={decisao.pedido_erp})"
        )
        _confirmar(
            client,
            conn,
            decisao.id,
            AcaoReconciliacao.CANCELAMENTO_PENDENTE_MANUAL,
            observacoes=decisao.motivo_decisao,
        )
        return

    # 2. Aprovado, mas data não mudou.
    if decisao.prazo_pactuado is None or decisao.prazo_pactuado == decisao.prazo_entrega_original:
        _confirmar(client, conn, decisao.id, AcaoReconciliacao.SEM_ACAO_NECESSARIA)
        return

    # 3. Aprovado com data nova → UPDATE no Fire (ou dry_run).
    if config.dry_run:
        logger.info(
            f"[DRY_RUN] UPDATE DT_ENTREGA={decisao.prazo_pactuado} pedido={decisao.pedido_erp}"
        )
        _confirmar(
            client,
            conn,
            decisao.id,
            AcaoReconciliacao.DATA_ATUALIZADA,
            fire_id_externo=decisao.pedido_erp,
            observacoes="DRY_RUN (sem escrita real no Fire)",
        )
        return

    try:
        rows = update_dt_entrega(
            fire_conn,
            pedido_cliente=decisao.pedido_erp,
            cliente_cnpj=decisao.cliente_cnpj,
            new_date_iso=decisao.prazo_pactuado,
            timezone=config.timezone,
        )
    except Exception as exc:  # timeout/lock — não confirma; re-tenta no próximo poll
        logger.error(f"flowpcp UPDATE Fire falhou decisao={decisao.id}: {exc}")
        return

    if rows == 0:
        attempts = flowpcp_repo.register_attempt(conn, decisao.id)
        if attempts >= _MAX_NAO_ENCONTRADO:
            logger.critical(
                f"flowpcp pedido {decisao.pedido_erp} não localizado no Fire "
                f"após {attempts} tentativas"
            )
            _confirmar(
                client,
                conn,
                decisao.id,
                AcaoReconciliacao.PEDIDO_NAO_ENCONTRADO_NO_FIRE,
                observacoes=f"{attempts} tentativas",
            )
        return

    _confirmar(
        client,
        conn,
        decisao.id,
        AcaoReconciliacao.DATA_ATUALIZADA,
        fire_id_externo=decisao.pedido_erp,
        observacoes=f"UPDATE OK (rows={rows})",
    )


def poll_decisoes_once(
    *,
    client: FlowPCPClient,
    fire_conn,
    conn: sqlite3.Connection,
    config: FlowPCPConfig,
) -> int:
    if not config.enabled:
        return 0
    cursor = flowpcp_repo.get_last_cursor(conn)
    resp = client.list_decisoes(cursor=cursor, limit=50)
    for decisao in resp.decisoes:
        try:
            processar_decisao(
                decisao, client=client, fire_conn=fire_conn, conn=conn, config=config
            )
        except Exception as exc:  # noqa: BLE001 — uma decisão ruim não derruba o lote
            logger.error(f"flowpcp erro processando decisão {decisao.id}: {exc}")
    if resp.proximo_cursor:
        flowpcp_repo.save_last_cursor(conn, resp.proximo_cursor)
    return len(resp.decisoes)
