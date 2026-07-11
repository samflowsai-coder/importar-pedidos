from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime

from app.erp.catalog_extract import extract_produtos
from app.erp.connection import FirebirdConnection
from app.integrations.flowpcp.catalogo_mapper import build_catalogo_request
from app.integrations.flowpcp.catalogo_schema import CatalogoReconciliacaoResponse
from app.integrations.flowpcp.client import FlowPCPClient
from app.integrations.flowpcp.config import flowpcp_config_for_slug
from app.persistence import catalogo_fire_repo, environments_repo, router
from app.utils.logger import logger

_IMPORTADOR_VERSAO = "1.0.0"


@dataclass(frozen=True)
class CatalogoLocalResult:
    """Resultado do sync quando o envio ao Flow está DESLIGADO
    (flowpcp_catalogo_push=0): a extração foi gravada só na cópia local."""

    itens: int
    extraido_em: str


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
    _env_conn=None,
) -> CatalogoReconciliacaoResponse | CatalogoLocalResult | None:
    """Extrai o catálogo do Fire do ambiente `slug`, SEMPRE grava a cópia local
    (`catalogo_fire` no db do ambiente) e — só se `flowpcp_catalogo_push` estiver
    ligado — empurra ao FlowPCP.

    Retorna:
    - `CatalogoReconciliacaoResponse` — enviado ao Flow (gate ON)
    - `CatalogoLocalResult` — só cópia local atualizada (gate OFF)
    - `None` — ambiente sem FlowPCP habilitado
    `_client`/`_fire_conn`/`_env_conn` são injeção de teste (default constrói os reais)."""
    cfg = flowpcp_config_for_slug(slug)
    if cfg is None or not getattr(cfg, "enabled", False):
        logger.info(f"catalogo sync: ambiente {slug} sem FlowPCP habilitado — skip")
        return None

    extraido_em = now_iso or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    if _fire_conn is not None:
        fire_ctx = nullcontext(_fire_conn)
    else:
        env = environments_repo.get_by_slug(slug)
        fire_ctx = FirebirdConnection().connect_with_config(environments_repo.to_fb_config(env))

    with fire_ctx as fire_conn:
        dtos = extract_produtos(fire_conn)

    # Cópia local SEMPRE — "manter no importador" independe do envio ao Flow.
    env_ctx = nullcontext(_env_conn) if _env_conn is not None else router.env_connect(slug)
    with env_ctx as env_conn:
        catalogo_fire_repo.replace_all(env_conn, dtos, extraido_em=extraido_em)
    logger.info(f"catalogo sync env={slug} itens={len(dtos)} — cópia local atualizada")

    if not getattr(cfg, "catalogo_push", False):
        logger.info(f"catalogo sync env={slug}: envio ao Flow DESLIGADO (catalogo_push=0)")
        return CatalogoLocalResult(itens=len(dtos), extraido_em=extraido_em)

    client = _client or _build_client(cfg)
    try:
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
