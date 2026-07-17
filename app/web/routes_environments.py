"""Admin CRUD de ambientes — `/api/admin/environments/*`.

Todas as rotas exigem usuário admin (`require_admin`). Não exigem
ambiente selecionado: é exatamente daqui que o admin cria/edita os
ambientes que outros usuários poderão selecionar.

Senha do Firebird:
- Cifrada via `secret_store` ao gravar (POST/PATCH)
- Nunca retornada (GET retorna `public_view` sem `fb_password_enc`)
- PATCH: `fb_password=None` mantém atual, `""` limpa, valor substitui

Endpoint `POST /{id}/test` valida pastas + tenta conexão Firebird.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

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


class FlowPCPConfigRequest(BaseModel):
    """Config da ponte FlowPCP por ambiente. Token cifrado via secret_store."""

    enabled: bool = False
    base_url: str | None = None
    tenant_id: str | None = None
    timezone: str = "America/Sao_Paulo"
    dry_run: bool = False
    poll_interval_s: int = Field(default=30, ge=5)
    request_timeout_s: float = Field(default=30.0, gt=0)
    # Gate do envio de catálogo ao Flow (OFF = sync só atualiza a cópia local)
    catalogo_push: bool = False
    # Filtro da extração: OFF = todo PRODUTOS (hoje); ON = só subgrupo MEIAS
    # (depende da marcação no Fire — Parte 2 do rollout)
    catalogo_apenas_meias: bool = False
    # Gate do envio de clientes ao Flow (OFF = sync só atualiza a cópia local)
    clientes_push: bool = False
    # None = mantém token atual; "" = limpa; valor = substitui
    service_token: str | None = None


@router.get("")
def list_environments(_=Depends(require_admin)):
    return environments_repo.list_all()


@router.post("", status_code=201)
def create_environment(payload: CreateEnvRequest, _=Depends(require_admin)):
    try:
        return environments_repo.create(**payload.model_dump())
    except environments_repo.SlugTaken:
        raise HTTPException(409, "Slug já existe — escolha outro.") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/{env_id}")
def get_environment(env_id: str, _=Depends(require_admin)):
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(404, "Ambiente não encontrado")
    return env


@router.patch("/{env_id}")
def update_environment(env_id: str, payload: UpdateEnvRequest, _=Depends(require_admin)):
    if not environments_repo.get(env_id):
        raise HTTPException(404, "Ambiente não encontrado")
    return environments_repo.update(env_id, **payload.model_dump())


@router.put("/{env_id}/flowpcp")
def set_environment_flowpcp(env_id: str, payload: FlowPCPConfigRequest, _=Depends(require_admin)):
    """Grava a config FlowPCP do ambiente (token cifrado). `service_token`:
    omitir/None mantém o atual, "" limpa, valor substitui."""
    if not environments_repo.get(env_id):
        raise HTTPException(404, "Ambiente não encontrado")
    return environments_repo.set_flowpcp_config(env_id, **payload.model_dump())


@router.post("/{env_id}/flowpcp/sync-catalogo")
def sync_catalogo_flowpcp(env_id: str, apply: bool = False, _=Depends(require_admin)):
    """Full-load do catálogo (produtos Fire → FlowPCP), direção IDA.

    Lê TODOS os produtos (`PRODUTOS`) do Fire do ambiente e empurra pro Flow.
    - `apply=false` (default): **dry-run** — reconcilia e devolve o relatório,
      NÃO escreve no catálogo do Flow. Seguro pra rodar quando quiser.
    - `apply=true`: **promove** — o Flow linka + grava de verdade (Fase 1). Exige
      o `/catalogo` do Flow com o promote no ar (senão devolve 422 → 502 aqui).
    Blocking (Firebird + HTTP) → FastAPI roda esta rota `def` no threadpool.
    """
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(404, "Ambiente não encontrado")
    if not env.get("flowpcp_enabled"):
        raise HTTPException(409, "FlowPCP não está habilitado neste ambiente")

    from app.integrations.flowpcp.catalogo_sync import CatalogoLocalResult, run_catalogo_sync

    try:
        rep = run_catalogo_sync(env["slug"], dry_run=not apply, full_sync=True)
    except Exception as exc:  # noqa: BLE001 — vira erro HTTP legível pro operador
        raise HTTPException(502, f"Falha no sync de catálogo: {exc}") from exc
    if rep is None:
        raise HTTPException(409, "FlowPCP não está habilitado neste ambiente")
    if isinstance(rep, CatalogoLocalResult):
        # Gate OFF: catálogo atualizado só no importador — nada foi ao Flow.
        return {"local_only": True, "itens": rep.itens, "extraido_em": rep.extraido_em}
    return rep.model_dump()


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
