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
def update_environment(
    env_id: str, payload: UpdateEnvRequest, _=Depends(require_admin)
):
    if not environments_repo.get(env_id):
        raise HTTPException(404, "Ambiente não encontrado")
    return environments_repo.update(env_id, **payload.model_dump())


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
