from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.persistence import environments_repo


@dataclass(frozen=True)
class FlowPCPConfig:
    enabled: bool = False
    base_url: str = ""
    service_token: str = ""
    tenant_id: str = ""
    timezone: str = "America/Sao_Paulo"
    dry_run: bool = False
    poll_interval_s: int = 30
    request_timeout_s: float = 30.0
    # Gate do envio de catálogo ao Flow: OFF = sync só atualiza a cópia local.
    catalogo_push: bool = False


def flowpcp_config_from_env(env: dict[str, Any], *, service_token: str | None) -> FlowPCPConfig:
    """Materializa a FlowPCPConfig a partir das colunas `flowpcp_*` do ambiente
    (`environments_repo`) + o token já decifrado. Mapper puro (sem I/O)."""
    return FlowPCPConfig(
        enabled=bool(env.get("flowpcp_enabled")),
        base_url=str(env.get("flowpcp_base_url") or ""),
        service_token=service_token or "",
        tenant_id=str(env.get("flowpcp_tenant_id") or ""),
        timezone=str(env.get("flowpcp_timezone") or "America/Sao_Paulo"),
        dry_run=bool(env.get("flowpcp_dry_run")),
        poll_interval_s=int(env.get("flowpcp_poll_interval_s") or 30),
        request_timeout_s=float(env.get("flowpcp_request_timeout_s") or 30.0),
        catalogo_push=bool(env.get("flowpcp_catalogo_push")),
    )


def flowpcp_config_for_slug(slug: str) -> FlowPCPConfig | None:
    """Config FlowPCP ATIVA do ambiente `slug`, ou None se o ambiente não existe
    ou não tem FlowPCP habilitado. Decifra o service_token via secret_store."""
    env = environments_repo.get_by_slug(slug)
    if env is None:
        return None
    cfg = flowpcp_config_from_env(
        env, service_token=environments_repo.get_flowpcp_token(env["id"])
    )
    return cfg if cfg.enabled else None


def enabled_flowpcp_envs() -> dict[str, FlowPCPConfig]:
    """{slug: FlowPCPConfig} só dos ambientes ATIVOS com FlowPCP habilitado.
    Só o ambiente MM liga; Nasmar (só vende) fica de fora."""
    out: dict[str, FlowPCPConfig] = {}
    for env in environments_repo.list_active():
        if not env.get("flowpcp_enabled"):
            continue
        out[env["slug"]] = flowpcp_config_from_env(
            env, service_token=environments_repo.get_flowpcp_token(env["id"])
        )
    return out
