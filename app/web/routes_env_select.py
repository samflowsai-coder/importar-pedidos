"""Rotas de seleção do ambiente ativo.

Fluxo:
1. Após login, cliente vai para `/selecionar-ambiente` se não tiver
   cookie `portal_env` válido (redirecionamento feito por `/`).
2. UI carrega lista via `GET /api/env/list`.
3. Usuário clica → `POST /api/env/select` → cookie `portal_env` setado.
4. Cliente redireciona para `/`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.persistence import environments_repo
from app.web.auth import require_user, set_env_cookie

router = APIRouter()


class SelectEnvRequest(BaseModel):
    environment_id: str


@router.get("/api/env/list")
def list_envs(_=Depends(require_user)):
    """Ambientes ativos disponíveis para seleção."""
    return [
        {"id": e["id"], "slug": e["slug"], "name": e["name"]}
        for e in environments_repo.list_active()
    ]


@router.post("/api/env/select")
def select_env(payload: SelectEnvRequest, response: Response, _=Depends(require_user)):
    env = environments_repo.get(payload.environment_id)
    if not env or not env["is_active"]:
        raise HTTPException(404, "Ambiente não encontrado")
    set_env_cookie(response, env["id"])
    return {
        "ok": True,
        "environment": {"id": env["id"], "slug": env["slug"], "name": env["name"]},
    }
