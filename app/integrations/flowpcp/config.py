from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass


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


def load_flowpcp_config(env: dict) -> FlowPCPConfig:
    """Lê a sub-seção `flowpcp` do config do ambiente. Desligado por padrão.
    Só o ambiente MM preenche; Nasmar fica disabled."""
    raw = (env or {}).get("flowpcp") or {}
    return FlowPCPConfig(
        enabled=bool(raw.get("enabled", False)),
        base_url=str(raw.get("base_url", "")),
        service_token=str(raw.get("service_token", "")),
        tenant_id=str(raw.get("tenant_id", "")),
        timezone=str(raw.get("timezone", "America/Sao_Paulo")),
        dry_run=bool(raw.get("dry_run", False)),
        poll_interval_s=int(raw.get("poll_interval_s", 30)),
        request_timeout_s=float(raw.get("request_timeout_s", 30.0)),
    )


def load_flowpcp_envs(environ: Mapping[str, str] | None = None) -> dict[str, FlowPCPConfig]:
    """Carrega config FlowPCP por ambiente a partir da env var `FLOWPCP_ENVS`.

    Formato (JSON): ``{"<slug>": {"flowpcp": {...}}, ...}`` — mesma sub-seção que
    `load_flowpcp_config` lê. Fonte interina (env var/JSON) até a UI de config +
    secret_store por ambiente (follow-up §6 da spec). JSON inválido → {} (o poll
    fica desligado em vez de derrubar o worker)."""
    env = os.environ if environ is None else environ
    raw = (env.get("FLOWPCP_ENVS") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(slug): load_flowpcp_config(cfg) for slug, cfg in data.items()}
