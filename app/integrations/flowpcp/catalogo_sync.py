from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime

from app.erp.catalog_extract import extract_produtos
from app.erp.connection import FirebirdConnection
from app.integrations.flowpcp.catalogo_mapper import build_catalogo_request
from app.integrations.flowpcp.catalogo_schema import CatalogoReconciliacaoResponse
from app.integrations.flowpcp.client import FlowPCPClient
from app.integrations.flowpcp.config import flowpcp_config_for_slug
from app.persistence import environments_repo
from app.utils.logger import logger

_IMPORTADOR_VERSAO = "1.0.0"


def _build_client(cfg) -> FlowPCPClient:
    return FlowPCPClient(
        base_url=cfg.base_url,
        service_token=cfg.service_token,
        tenant_id=cfg.tenant_id,
        timeout=cfg.request_timeout_s,
    )


def run_catalogo_sync(
    slug: str,
    *,
    dry_run: bool = True,
    full_sync: bool = True,
    now_iso: str | None = None,
    _client=None,
    _fire_conn=None,
) -> CatalogoReconciliacaoResponse | None:
    """Extrai o catálogo do Fire do ambiente `slug` e empurra ao FlowPCP.
    Fase 0: dry_run=True (não promove). Retorna o relatório, ou None se o
    ambiente não tem FlowPCP habilitado. `_client`/`_fire_conn` são injeção
    de teste (default constrói os reais)."""
    cfg = flowpcp_config_for_slug(slug)
    if cfg is None or not getattr(cfg, "enabled", False):
        logger.info(f"catalogo sync: ambiente {slug} sem FlowPCP habilitado — skip")
        return None

    extraido_em = now_iso or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    client = _client or _build_client(cfg)

    if _fire_conn is not None:
        fire_ctx = nullcontext(_fire_conn)
    else:
        env = environments_repo.get_by_slug(slug)
        fire_ctx = FirebirdConnection().connect_with_config(environments_repo.to_fb_config(env))

    try:
        with fire_ctx as fire_conn:
            dtos = extract_produtos(fire_conn)
        request = build_catalogo_request(
            dtos,
            dry_run=dry_run,
            full_sync=full_sync,
            importador_versao=_IMPORTADOR_VERSAO,
            extraido_em=extraido_em,
        )
        logger.info(
            f"catalogo sync env={slug} itens={len(dtos)} dry_run={dry_run} full_sync={full_sync}"
        )
        return client.send_catalogo(request)
    finally:
        if _client is None:
            client.close()
