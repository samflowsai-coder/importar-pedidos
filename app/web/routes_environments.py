"""Admin CRUD de ambientes — `/api/admin/environments/*`.

Todas as rotas exigem usuário admin (`require_admin`). Não exigem
ambiente selecionado: é exatamente daqui que o admin cria/edita os
ambientes que outros usuários poderão selecionar.

Senha do Firebird:
- Cifrada via `secret_store` ao gravar (POST/PATCH)
- Nunca retornada (GET retorna `public_view` sem `fb_password_enc`)
- PATCH: `fb_password=None` mantém atual, `""` limpa, valor substitui

Endpoint `POST /{id}/test` valida pastas + tenta conexão Firebird.
Endpoint `POST /{id}/flowpcp/test` verifica credenciais FlowPCP via health check.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.integrations.flowpcp.client import FlowPCPClient
from app.persistence import environments_repo
from app.web.auth import require_admin

router = APIRouter(prefix="/api/admin/environments", tags=["admin", "environments"])


class CreateEnvRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=31)
    name: str = Field(..., min_length=1)
    watch_dir: str = Field(..., min_length=1)
    output_dir: str = Field(..., min_length=1)
    fb_path: str = Field(..., min_length=1)
    fb_host: str | None = None
    fb_port: str | None = None
    fb_user: str = "SYSDBA"
    fb_charset: str = "WIN1252"
    fb_password: str | None = None
    # FlowPCP integration (optional at create time)
    flowpcp_enabled: bool | None = None
    flowpcp_base_url: str | None = None
    flowpcp_tenant_id: str | None = None
    flowpcp_api_key: str | None = None


class UpdateEnvRequest(BaseModel):
    """slug propositalmente ausente — imutável após criação."""
    name: str | None = None
    watch_dir: str | None = None
    output_dir: str | None = None
    fb_path: str | None = None
    fb_host: str | None = None
    fb_port: str | None = None
    fb_user: str | None = None
    fb_charset: str | None = None
    # None = mantém senha atual; "" = limpa; valor = substitui
    fb_password: str | None = None
    # FlowPCP integration (all optional; None = keep existing)
    flowpcp_enabled: bool | None = None
    flowpcp_base_url: str | None = None
    flowpcp_tenant_id: str | None = None
    # None = keep existing key; "" = clear key; value = replace
    flowpcp_api_key: str | None = None


@router.get("")
def list_environments(_=Depends(require_admin)):
    return environments_repo.list_all()


@router.post("", status_code=201)
def create_environment(payload: CreateEnvRequest, _=Depends(require_admin)):
    fb_fields = {
        k: v for k, v in payload.model_dump().items()
        if not k.startswith("flowpcp_")
    }
    try:
        env = environments_repo.create(**fb_fields)
    except environments_repo.SlugTaken:
        raise HTTPException(409, "Slug já existe — escolha outro.") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if any(v is not None for v in (
        payload.flowpcp_enabled, payload.flowpcp_base_url,
        payload.flowpcp_tenant_id, payload.flowpcp_api_key,
    )):
        env = environments_repo.set_flowpcp_config(
            env_id=env["id"],
            enabled=bool(payload.flowpcp_enabled),
            base_url=payload.flowpcp_base_url,
            tenant_id=payload.flowpcp_tenant_id,
            api_key=payload.flowpcp_api_key,
        )
    return env


@router.get("/{env_id}")
def get_environment(env_id: str, _=Depends(require_admin)):
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(404, "Ambiente não encontrado")
    return env


@router.patch("/{env_id}")
def update_environment(
    env_id: str, payload: UpdateEnvRequest, _=Depends(require_admin)
):
    if not environments_repo.get(env_id):
        raise HTTPException(404, "Ambiente não encontrado")
    fb_fields = {
        k: v for k, v in payload.model_dump().items()
        if not k.startswith("flowpcp_")
    }
    env = environments_repo.update(env_id, **fb_fields)
    if any(v is not None for v in (
        payload.flowpcp_enabled, payload.flowpcp_base_url,
        payload.flowpcp_tenant_id, payload.flowpcp_api_key,
    )):
        env = environments_repo.set_flowpcp_config(
            env_id=env_id,
            enabled=bool(payload.flowpcp_enabled),
            base_url=payload.flowpcp_base_url,
            tenant_id=payload.flowpcp_tenant_id,
            api_key=payload.flowpcp_api_key,
        )
    return env


@router.delete("/{env_id}", status_code=204)
def delete_environment(env_id: str, _=Depends(require_admin)):
    if not environments_repo.get(env_id):
        raise HTTPException(404, "Ambiente não encontrado")
    environments_repo.soft_delete(env_id)
    return Response(status_code=204)


@router.post("/{env_id}/test")
def test_environment(env_id: str, _=Depends(require_admin)):
    """Valida pastas + tenta conexão Firebird. Não levanta — retorna status."""
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(404, "Ambiente não encontrado")

    watch_ok = Path(env["watch_dir"]).is_dir()
    output_ok = Path(env["output_dir"]).is_dir()
    fb_ok, fb_err = _try_firebird(env)

    return {
        "watch_dir": env["watch_dir"],
        "watch_dir_ok": watch_ok,
        "output_dir": env["output_dir"],
        "output_dir_ok": output_ok,
        "firebird_ok": fb_ok,
        "firebird_error": fb_err,
    }


@router.post("/{env_id}/flowpcp/test")
def test_flowpcp_connection(env_id: str, _=Depends(require_admin)):
    """Verifica credenciais FlowPCP via health check. Não levanta — retorna status."""
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(404, "Ambiente não encontrado")
    cfg = environments_repo.to_flowpcp_config(env)
    if not cfg["enabled"] or not all([cfg["base_url"], cfg["tenant_id"], cfg["api_key"]]):
        return {"ok": False, "reason": "incomplete_config"}
    client = FlowPCPClient(
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        tenant_id=cfg["tenant_id"],
    )
    try:
        ok = client.health()
    finally:
        client.close()
    return {"ok": ok}


def _try_firebird(env: dict) -> tuple[bool, str | None]:
    try:
        from app.erp.connection import FirebirdConnection
        cfg = environments_repo.to_fb_config(env)
        with FirebirdConnection().connect_with_config(cfg) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM RDB$DATABASE")
            cur.fetchone()
        return True, None
    except Exception as e:  # noqa: BLE001 — explicitly capturing all errors for diagnostics
        return False, str(e)
