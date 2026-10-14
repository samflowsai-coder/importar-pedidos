"""Envio ao FlowPCP de dentro do processo web (`ui.py`).

No Windows do cliente só o `ui.py` roda (`scripts/setup-service.ps1`); o worker
(`python -m app.worker`), onde vivem o drain do outbox e o poll de decisões,
nunca subiu lá: o último pedido da MM chegou ao Flow pelo `/recebimento` em
24/07/2026. Esta thread faz as duas coisas no processo que já roda, como a
reconciliação e o SP1 (`app/web/server.py`).

- **Fila do `/recebimento`**: a cada `TICK_S`, só o target flowpcp, só nos
  ambientes com FlowPCP habilitado. O Gestor continua no worker.
- **Fila acumulada**: linha com mais de `LIMITE_ATRASO` só vai se o pedido segue
  aberto no ERP (`imports.fire_codigo` → `CAB_VENDAS.STATUS`, leitura READ
  ONLY). Faturado, cancelado, apagado do ERP ou sem código conhecido sai da fila
  com o motivo e não chega ao Flow: pedido encerrado virando demanda nova é pior
  que não mandar (os abertos chegam ao Flow pelo SP1 de qualquer jeito). ERP
  fora do ar: a linha antiga espera o próximo ciclo; as recentes seguem.
- **Decisões do Flow** (escrita de `DT_ENTREGA` no ERP): só com
  `PORTAL_FLOWPCP_DECISOES=1`; desligado por padrão. O "Dry-run" da tela não
  serve de interruptor: ele CONFIRMA a decisão no Flow sem gravar no ERP.

`PORTAL_FLOWPCP_ENVIO=0` desliga a thread inteira (deploy docker, onde o worker
roda e faria o mesmo trabalho).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from app.erp import queries
from app.erp.connection import FirebirdConnection
from app.integrations.flowpcp.client import FLOWPCP_TARGET_NAME
from app.integrations.flowpcp.config import FlowPCPConfig, enabled_flowpcp_envs
from app.persistence import context as env_context
from app.persistence import db, environments_repo, outbox_repo, repo
from app.persistence.outbox_repo import OutboxRow
from app.utils.logger import logger
from app.worker.jobs.drain_outbox import _process_flowpcp_row
from app.worker.jobs.poll_flowpcp import run_poll_flowpcp

TICK_S = 15.0
INTERVALO_DECISOES = timedelta(seconds=30)
LIMITE_ATRASO = timedelta(days=7)
LOTE = 20
_ENCERRADOS = frozenset({"FATURADO", "CANCELADO"})
_BLOCO_ERP = 200

LerStatus = Callable[[dict, list[int]], dict[int, str]]


def envio_habilitado() -> bool:
    return os.environ.get("PORTAL_FLOWPCP_ENVIO", "1").strip() != "0"


def decisoes_habilitadas() -> bool:
    return os.environ.get("PORTAL_FLOWPCP_DECISOES", "0").strip() == "1"


def ler_status_erp(env: dict, codigos: list[int]) -> dict[int, str]:
    """`CAB_VENDAS.CODIGO` → STATUS (maiúsculo). Código ausente = apagado do ERP."""
    saida: dict[int, str] = {}
    with FirebirdConnection().connect_somente_leitura(environments_repo.to_fb_config(env)) as fb:
        cur = fb.cursor()
        try:
            for i in range(0, len(codigos), _BLOCO_ERP):
                bloco = codigos[i : i + _BLOCO_ERP]
                cur.execute(queries.pedidos_erp_por_codigo_sql(len(bloco)), bloco)
                for r in cur.fetchall():
                    saida[int(r[0])] = (r[4] or "").strip().upper()
        finally:
            cur.close()
    return saida


def _codigos_erp(import_ids: list[str]) -> dict[str, int | None]:
    marcas = ", ".join("?" for _ in import_ids)
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT id, fire_codigo FROM imports WHERE id IN ({marcas})", import_ids
        ).fetchall()
    return {r["id"]: r["fire_codigo"] for r in rows}


def _descartar(row: OutboxRow, motivo: str, detalhe: dict) -> None:
    outbox_repo.mark_discarded(row.id, reason=f"descartado: {motivo}")
    repo.append_audit(row.import_id, "flowpcp_descartado", {"motivo": motivo, **detalhe})


def drenar_ambiente(
    env: dict,
    cfg: FlowPCPConfig,
    *,
    agora: datetime | None = None,
    ler_status: LerStatus = ler_status_erp,
    enviar: Callable[[OutboxRow, FlowPCPConfig], None] = _process_flowpcp_row,
) -> dict[str, int]:
    """Um ciclo da fila flowpcp de um ambiente (com o contexto do ambiente ativo).

    Recentes primeiro: a fila acumulada nunca atrasa um pedido novo."""
    agora = agora or datetime.now()
    corte = (agora - LIMITE_ATRASO).isoformat(timespec="seconds")  # mesmo formato de created_at
    conta = {"enviados": 0, "descartados": 0, "aguardando_erp": 0}

    recentes = outbox_repo.list_due(FLOWPCP_TARGET_NAME, limit=LOTE, criado_depois=corte)
    for row in recentes:
        enviar(row, cfg)
        conta["enviados"] += 1

    vagas = LOTE - len(recentes)
    atrasadas = (
        outbox_repo.list_due(FLOWPCP_TARGET_NAME, limit=vagas, criado_ate=corte) if vagas else []
    )
    if not atrasadas:
        return conta

    codigos = _codigos_erp([r.import_id for r in atrasadas])
    conhecidos = sorted({int(c) for c in codigos.values() if c is not None})
    status: dict[int, str] | None = {}
    if conhecidos:
        try:
            status = ler_status(env, conhecidos)
        except Exception as exc:  # noqa: BLE001 — ERP fora: a linha antiga espera o próximo ciclo
            logger.warning(f"flowpcp.envio env={env['slug']}: leitura do ERP falhou: {exc!r}")
            status = None

    for row in atrasadas:
        codigo = codigos.get(row.import_id)
        if codigo is None:
            _descartar(row, "fila acumulada sem pedido conhecido no ERP", {"fire_codigo": None})
            conta["descartados"] += 1
            continue
        if status is None:
            conta["aguardando_erp"] += 1
            continue
        situacao = status.get(int(codigo))
        if situacao is None or situacao in _ENCERRADOS:
            _descartar(
                row,
                f"fila acumulada, pedido {situacao or 'apagado'} no ERP",
                {"fire_codigo": int(codigo), "status": situacao},
            )
            conta["descartados"] += 1
            continue
        enviar(row, cfg)
        conta["enviados"] += 1
    return conta


def drenar() -> None:
    for slug, cfg in enabled_flowpcp_envs().items():
        env = environments_repo.get_by_slug(slug)
        if env is None:
            continue
        try:
            with env_context.active_env(env["id"], env["slug"]):
                conta = drenar_ambiente(env, cfg)
        except Exception as exc:  # noqa: BLE001 — um ambiente ruim não derruba os outros
            logger.error(f"flowpcp.envio env={slug} falhou: {exc!r}")
            continue
        if any(conta.values()):
            logger.info(f"flowpcp.envio env={slug} {conta}")


def ciclo(ultima_decisao: datetime, agora: datetime) -> datetime:
    """Uma volta da thread: drena a fila; com o interruptor ligado, lê as
    decisões do Flow a cada `INTERVALO_DECISOES`. Devolve a hora da última leitura."""
    try:
        drenar()
    except Exception as exc:  # noqa: BLE001 — a thread daemon não pode morrer
        logger.error(f"flowpcp.envio: drenagem falhou: {exc!r}")
    if not decisoes_habilitadas() or agora - ultima_decisao < INTERVALO_DECISOES:
        return ultima_decisao
    try:
        run_poll_flowpcp()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"flowpcp.envio: leitura das decisões falhou: {exc!r}")
    return agora


def loop() -> None:
    logger.info(
        "flowpcp.envio: loop iniciado (decisões do Flow "
        f"{'LIGADAS' if decisoes_habilitadas() else 'desligadas'})"
    )
    ultima_decisao = datetime.min
    while True:
        time.sleep(TICK_S)
        ultima_decisao = ciclo(ultima_decisao, datetime.now())
