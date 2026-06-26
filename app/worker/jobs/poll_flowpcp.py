"""FlowPCP decisions poll job — runs every 30s via the worker scheduler.

Para CADA ambiente com FlowPCP habilitado (`FLOWPCP_ENVS`, gated aos ambientes
ativos), busca decisões pendentes no FlowPCP e reconcilia a data de entrega no
Fire (Modelo B / OVERLAY). Um ambiente ruim não derruba os outros.

Config interina via env var `FLOWPCP_ENVS` (JSON slug->config); a UI de config +
secret_store por ambiente é follow-up (§6 da spec). Só o ambiente MM liga hoje.
"""
from __future__ import annotations

from app.erp.connection import FirebirdConnection
from app.integrations.flowpcp.client import FlowPCPClient
from app.integrations.flowpcp.config import FlowPCPConfig, load_flowpcp_envs
from app.integrations.flowpcp.poll_decisoes import poll_decisoes_once
from app.persistence import environments_repo, router
from app.utils.logger import logger


def _list_flowpcp_envs() -> list[tuple[str, FlowPCPConfig]]:
    """(slug, config) só dos ambientes ATIVOS com flowpcp.enabled=true."""
    active = set(router.list_env_slugs())
    return [
        (slug, cfg)
        for slug, cfg in load_flowpcp_envs().items()
        if cfg.enabled and slug in active
    ]


def _open_env_conn(slug: str):
    """Conexão SQLite per-ambiente (context manager). Garante schema."""
    return router.env_connect(slug)


def _open_fire_conn(slug: str):
    """Conexão Firebird do ambiente (context manager) via creds do environments_repo."""
    env = environments_repo.get_by_slug(slug)
    if env is None:
        raise RuntimeError(f"ambiente {slug} não encontrado")
    return FirebirdConnection().connect_with_config(environments_repo.to_fb_config(env))


def _build_client(cfg: FlowPCPConfig) -> FlowPCPClient:
    return FlowPCPClient(
        base_url=cfg.base_url,
        service_token=cfg.service_token,
        tenant_id=cfg.tenant_id,
        timeout=cfg.request_timeout_s,
    )


def run_poll_flowpcp() -> None:
    for slug, cfg in _list_flowpcp_envs():
        client = _build_client(cfg)
        try:
            with _open_env_conn(slug) as conn, _open_fire_conn(slug) as fire_conn:
                n = poll_decisoes_once(
                    client=client, fire_conn=fire_conn, conn=conn, config=cfg
                )
            logger.info(f"flowpcp poll env={slug} decisoes={n}")
        except Exception as exc:  # noqa: BLE001 — um ambiente ruim não derruba os outros
            logger.error(f"flowpcp poll env={slug} falhou: {exc}")
        finally:
            client.close()
