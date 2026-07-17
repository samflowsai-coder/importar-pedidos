from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.erp.cliente_extract import extract_clientes_ativos
from app.erp.connection import FirebirdConnection
from app.integrations.flowpcp.client import FlowPCPClient
from app.integrations.flowpcp.clientes_mapper import build_clientes_request
from app.integrations.flowpcp.clientes_schema import ClientesReconciliacaoResponse
from app.integrations.flowpcp.config import flowpcp_config_for_slug
from app.persistence import clientes_fire_repo, environments_repo, router
from app.utils.logger import logger

_IMPORTADOR_VERSAO = "1.0.0"
_JANELA_DIAS = 365  # ~12 meses (hardcoded — YAGNI)


@dataclass(frozen=True)
class ClientesSyncResult:
    itens: int
    extraido_em: str
    descartados_cpf: int
    descartados_invalidos: int
    colisoes_dedup: int
    skipped_empty: bool = False
    reconciliacao: ClientesReconciliacaoResponse | None = None


def _build_client(cfg) -> FlowPCPClient:
    return FlowPCPClient(
        base_url=cfg.base_url,
        service_token=cfg.service_token,
        tenant_id=cfg.tenant_id,
        timeout=cfg.request_timeout_s,
    )


def run_clientes_sync(
    slug: str,
    *,
    dry_run: bool = True,
    full_sync: bool = False,  # I7: aditivo até a inativação existir
    now_iso: str | None = None,
    _hoje: date | None = None,
    permitir_vazio: bool = False,
    _client=None,
    _fire_conn=None,
    _env_conn=None,
) -> ClientesSyncResult | None:
    """Extrai clientes ativos (12m) do Fire do ambiente `slug`, grava a cópia
    local e — só se `flowpcp_clientes_push` estiver ligado — empurra ao Flow.

    Retorna `ClientesSyncResult` (com contadores em todos os caminhos) ou `None`
    se o ambiente não tem FlowPCP habilitado. `reconciliacao is None` ⇒ local-only.
    Trava I4: extração vazia não zera o snapshot nem envia (salvo `permitir_vazio`).
    """
    cfg = flowpcp_config_for_slug(slug)
    if cfg is None or not getattr(cfg, "enabled", False):
        logger.info(f"clientes sync: ambiente {slug} sem FlowPCP habilitado — skip")
        return None

    extraido_em = now_iso or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    hoje = _hoje or datetime.now(ZoneInfo(cfg.timezone)).date()
    desde = hoje - timedelta(days=_JANELA_DIAS)

    if _fire_conn is not None:
        fire_ctx = nullcontext(_fire_conn)
    else:
        env = environments_repo.get_by_slug(slug)
        fire_ctx = FirebirdConnection().connect_with_config(environments_repo.to_fb_config(env))

    with fire_ctx as fire_conn:
        extr = extract_clientes_ativos(fire_conn, desde_data=desde)

    logger.info(
        f"clientes sync env={slug} ativos={len(extr.clientes)} "
        f"descartados_cpf={extr.descartados_cpf} descartados_invalidos={extr.descartados_invalidos} "
        f"colisoes_dedup={extr.colisoes_dedup} desde={desde}"
    )

    # I4 — trava de vazio: não zera o snapshot local nem manda 0 itens ao Flow.
    if not extr.clientes and not permitir_vazio:
        logger.warning(
            f"clientes sync env={slug}: extração VAZIA — snapshot preservado, nada enviado "
            f"(use permitir_vazio=True para zerar de propósito)"
        )
        return ClientesSyncResult(
            itens=0,
            extraido_em=extraido_em,
            descartados_cpf=extr.descartados_cpf,
            descartados_invalidos=extr.descartados_invalidos,
            colisoes_dedup=extr.colisoes_dedup,
            skipped_empty=True,
        )

    env_ctx = nullcontext(_env_conn) if _env_conn is not None else router.env_connect(slug)
    with env_ctx as env_conn:
        clientes_fire_repo.replace_all(env_conn, extr.clientes, extraido_em=extraido_em)

    base = dict(
        itens=len(extr.clientes),
        extraido_em=extraido_em,
        descartados_cpf=extr.descartados_cpf,
        descartados_invalidos=extr.descartados_invalidos,
        colisoes_dedup=extr.colisoes_dedup,
    )

    if not getattr(cfg, "clientes_push", False):
        logger.info(f"clientes sync env={slug}: envio ao Flow DESLIGADO (clientes_push=0)")
        return ClientesSyncResult(**base)

    client = _client or _build_client(cfg)
    try:
        request = build_clientes_request(
            extr.clientes,
            dry_run=dry_run,
            full_sync=full_sync,
            importador_versao=_IMPORTADOR_VERSAO,
            extraido_em=extraido_em,
        )
        rep = client.send_clientes(request)
        return ClientesSyncResult(**base, reconciliacao=rep)
    finally:
        if _client is None:
            client.close()
