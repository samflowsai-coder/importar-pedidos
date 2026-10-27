"""Envio ao FlowPCP de dentro do processo web (`ui.py`).

No Windows do cliente só o `ui.py` roda (`scripts/setup-service.ps1`); o worker
(`python -m app.worker`), onde viviam o drain do outbox e o poll de decisões,
nunca subiu lá: o último pedido da MM chegou ao Flow pelo `/recebimento` em
24/07/2026. Esta thread faz as duas coisas no processo que já roda, como a
reconciliação e o SP1 (`app/web/server.py`). O worker usa a MESMA regra
(`drain_outbox` chama `drenar_ambiente`), então rodar os dois não muda o que
vai ao Flow.

Regra de cada linha da fila (`target=flowpcp`):

- **Pedido com código no ERP** (`imports.fire_codigo`), qualquer idade: só vai
  se segue aberto (`CAB_VENDAS.STATUS`, leitura READ ONLY, uma por ciclo).
  Faturado, cancelado ou apagado do ERP → `dead` com `last_error =
  "descartado: …"` e audit `flowpcp_descartado`: pedido encerrado virando
  demanda nova no Flow é pior que não mandar.
- **Sem código, recente** (até `LIMITE_ATRASO`): vai. No modo xlsx o pedido
  ainda nem entrou no ERP.
- **Sem código, acumulada**: fica `retido` (nem vai, nem morre) e volta sozinha
  para a fila quando a reconciliação achar o pedido no ERP
  (`outbox_repo.release_retained`).
- ERP fora: linha com código espera (pausa de `PAUSA_ERP` antes de tentar ler
  de novo); linha sem código recente segue.
- ERP que devolve NENHUM de vários códigos (caminho do banco trocado, cópia
  errada) não é "todos apagados": a leva espera, com log de erro.
- Recentes antes das acumuladas. Falha de envio (Flow fora, credencial errada)
  pausa o ambiente por `PAUSA_FLOW`: as outras linhas não queimam tentativa.
  Linha nunca morre por falha passageira; recusa definitiva do Flow (4xx de
  conteúdo) ou payload inválido vira `dead` com `recusado: …` (`drain_outbox`).

**Sem janela** (decisão do Samuel, 27/10/2026): a fila sai em todo tick, a
qualquer hora e dia. Pedido com preço a aprovar precisa estar no Flow para a
aprovação acontecer, então o pedido importado não espera horário. O que segura
o envio é só a regra de cada linha acima e as pausas `PAUSA_ERP`/`PAUSA_FLOW`.
O worker usa a mesma regra, sem portão de horário.

**Decisões do Flow** (escrita de `DT_ENTREGA` no ERP): só com
`PORTAL_FLOWPCP_DECISOES=1` (`config.decisoes_habilitadas`), desligado por padrão.

**Catálogo do Fire**: a mesma thread manda o catálogo inteiro ao Flow uma vez
por dia (`catalogo_diario`, 5h, só onde "Enviar catálogo ao Flow" está ligado).

**Trava de preço** (`precos.rodar`): tabela a cada 60 s e, em `ligado`, revisões
e decisões a cada 30 s. `PORTAL_FLOWPCP_PRECOS=0` desliga.

`PORTAL_FLOWPCP_ENVIO=0` desliga a thread inteira (deploy docker, onde o worker
faz o mesmo trabalho).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from app.erp import queries
from app.erp.connection import FirebirdConnection
from app.integrations.flowpcp import cadastro_volta, catalogo_diario, precos
from app.integrations.flowpcp.client import FLOWPCP_TARGET_NAME
from app.integrations.flowpcp.config import (
    FlowPCPConfig,
    decisoes_habilitadas,
    enabled_flowpcp_envs,
)
from app.observability.metrics import update_outbox_metrics
from app.persistence import context as env_context
from app.persistence import db, environments_repo, outbox_repo, repo
from app.persistence.outbox_repo import OutboxRow
from app.utils.janelas_envio import avisar_variavel_obsoleta
from app.utils.logger import logger
from app.worker.jobs.drain_outbox import _process_flowpcp_row
from app.worker.jobs.poll_flowpcp import run_poll_flowpcp

TICK_S = 15.0
INTERVALO_DECISOES = timedelta(seconds=30)
LIMITE_ATRASO = timedelta(days=7)
PAUSA_ERP = timedelta(minutes=5)
PAUSA_FLOW = timedelta(minutes=5)
LOTE = 20
_ENCERRADOS = frozenset({"FATURADO", "CANCELADO"})
_BLOCO_ERP = 200
_RETIDO = (
    "retido: fila acumulada sem pedido conhecido no ERP; volta sozinho quando "
    "a reconciliação achar o pedido"
)

LerStatus = Callable[[dict, list[int]], dict[int, str]]

# slug → até quando não tentar ler o ERP de novo (o fdb não tem timeout curto).
_pausa_erp: dict[str, datetime] = {}
# slug → até quando não tentar enviar de novo depois de uma falha de envio.
_pausa_flow: dict[str, datetime] = {}


def envio_habilitado() -> bool:
    return os.environ.get("PORTAL_FLOWPCP_ENVIO", "1").strip() != "0"


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
    return {
        r["id"]: (int(r["fire_codigo"]) if r["fire_codigo"] is not None else None) for r in rows
    }


def _status_do_erp(
    env: dict, codigos: list[int], agora: datetime, ler_status: LerStatus
) -> dict[int, str] | None:
    if not codigos:
        return {}
    slug = env["slug"]
    if _pausa_erp.get(slug, datetime.min) > agora:
        return None
    try:
        lidos = ler_status(env, codigos)
    except Exception as exc:  # noqa: BLE001 — ERP fora: as linhas com código esperam
        _pausa_erp[slug] = agora + PAUSA_ERP
        logger.warning(
            f"flowpcp.envio env={slug}: leitura do ERP falhou, nova tentativa em "
            f"{int(PAUSA_ERP.total_seconds() // 60)} min: {exc!r}"
        )
        return None
    if len(codigos) > 1 and not lidos:
        # Nenhum de vários pedidos existe? Mais provável o banco errado que todos
        # apagados — "apagado" descarta para sempre, então espera e grita.
        _pausa_erp[slug] = agora + PAUSA_ERP
        logger.error(
            f"flowpcp.envio env={slug}: o ERP não achou NENHUM de {len(codigos)} pedidos "
            "— confira o banco configurado no ambiente; nada foi descartado"
        )
        return None
    return lidos


def _descartar(row: OutboxRow, motivo: str, detalhe: dict) -> None:
    outbox_repo.mark_discarded(row.id, reason=f"descartado: {motivo}")
    repo.append_audit(row.import_id, "flowpcp_descartado", {"motivo": motivo, **detalhe})


def _situacao(row_id: int) -> str | None:
    atual = outbox_repo.get(row_id)
    return atual.status if atual else None


def drenar_ambiente(
    env: dict,
    cfg: FlowPCPConfig,
    *,
    agora: datetime | None = None,
    ler_status: LerStatus = ler_status_erp,
    enviar: Callable[[OutboxRow, FlowPCPConfig], None] = _process_flowpcp_row,
) -> dict[str, int]:
    """Um ciclo da fila flowpcp de um ambiente (com o contexto do ambiente ativo)."""
    agora = agora or datetime.now()
    corte = (agora - LIMITE_ATRASO).isoformat(timespec="seconds")  # formato de created_at
    conta = {
        "enviados": 0,
        "recusados": 0,
        "descartados": 0,
        "retidos": 0,
        "aguardando_erp": 0,
        "falhas": 0,
    }
    conta["liberados"] = outbox_repo.release_retained(FLOWPCP_TARGET_NAME)
    if _pausa_flow.get(env["slug"], datetime.min) > agora:
        return conta

    recentes = outbox_repo.list_due(FLOWPCP_TARGET_NAME, limit=LOTE, criado_depois=corte)
    vagas = LOTE - len(recentes)
    atrasadas = (
        outbox_repo.list_due(FLOWPCP_TARGET_NAME, limit=vagas, criado_ate=corte) if vagas else []
    )
    linhas = recentes + atrasadas
    if not linhas:
        return conta

    ids_atrasadas = {r.id for r in atrasadas}
    codigos = _codigos_erp([r.import_id for r in linhas])
    conhecidos = sorted({c for c in codigos.values() if c is not None})
    status = _status_do_erp(env, conhecidos, agora, ler_status)

    for row in linhas:
        codigo = codigos.get(row.import_id)
        if codigo is None:
            if row.id in ids_atrasadas:
                if outbox_repo.mark_retained(row.id, reason=_RETIDO):
                    repo.append_audit(row.import_id, "flowpcp_retido", {"motivo": _RETIDO})
                conta["retidos"] += 1
                continue
        elif status is None:
            conta["aguardando_erp"] += 1
            continue
        else:
            situacao = status.get(codigo)
            if situacao is None or situacao in _ENCERRADOS:
                _descartar(
                    row,
                    f"pedido {situacao or 'apagado'} no ERP",
                    {"fire_codigo": codigo, "status": situacao},
                )
                conta["descartados"] += 1
                continue
        enviar(row, cfg)
        depois = _situacao(row.id)
        if depois == "pending":
            # Flow fora ou credencial errada: pausa o ambiente em vez de queimar
            # uma tentativa de cada linha da fila.
            _pausa_flow[env["slug"]] = agora + PAUSA_FLOW
            conta["falhas"] += 1
            break
        conta["recusados" if depois == "dead" else "enviados"] += 1
    return conta


def drenar() -> None:
    """Drena a fila de cada ambiente habilitado, em toda chamada (sem janela)."""
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
    update_outbox_metrics()


def ciclo(ultima_decisao: datetime, agora: datetime) -> datetime:
    """Uma volta da thread: drena a fila; com o interruptor ligado, lê as
    decisões do Flow a cada `INTERVALO_DECISOES`. Devolve a hora da última leitura."""
    try:
        drenar()
    except Exception as exc:  # noqa: BLE001 — a thread daemon não pode morrer
        logger.error(f"flowpcp.envio: drenagem falhou: {exc!r}")
    try:
        catalogo_diario.rodar()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"flowpcp.envio: catálogo diário falhou: {exc!r}")
    try:
        cadastro_volta.rodar_se_devido(agora)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"flowpcp.envio: volta de cadastro falhou: {exc!r}")
    if precos.habilitado():
        try:
            precos.rodar(agora)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"flowpcp.envio: trava de preço falhou: {exc!r}")
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
    avisar_variavel_obsoleta()
    ultima_decisao = datetime.min
    while True:
        time.sleep(TICK_S)
        ultima_decisao = ciclo(ultima_decisao, datetime.now())
