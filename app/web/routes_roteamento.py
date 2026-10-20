"""Interruptor do roteamento e a evidência que autoriza virar a chave.

Três estados porque o problema é de sequência: não dá para validar o que não
está rodando, nem ligar o que não foi validado. `observando` roda o roteador e
grava o que ele teria feito, sem agir — e as **divergências** que saem daí são
o material de treinamento do time.

Quem vira a chave é admin. Não é configuração de operador.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.persistence import (
    environments_repo,
    regra_faturamento_repo,
    roteamento_decisao_repo,
    roteamento_repo,
)
from app.persistence import router as db_router
from app.web.auth import User, require_admin, require_user

router = APIRouter()


class ModoRequest(BaseModel):
    modo: str


@router.get("/api/roteamento/modo")
def get_modo(_=Depends(require_user)):
    return {"modo": roteamento_repo.modo(), "modos": list(roteamento_repo.MODOS)}


@router.put("/api/roteamento/modo")
def put_modo(payload: ModoRequest, user: User = Depends(require_admin)):
    try:
        roteamento_repo.set_modo(payload.modo, por=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"modo": roteamento_repo.modo()}


@router.get("/api/roteamento/taxa")
def get_taxa(dias: int = 30, _=Depends(require_user)):
    t = roteamento_repo.taxa(dias=dias)
    with db_router.shared_connect() as conn:
        rows = conn.execute(
            """SELECT import_id, decidido_em, degrau, env_sugerido,
                      env_escolhido_pelo_operador
               FROM roteamento_sombra
               WHERE bateu = 0 AND degrau <> 'perguntar' AND decidido_em >= ?
               ORDER BY decidido_em DESC LIMIT 50""",
            (t["desde"],),
        ).fetchall()
    t["divergencias"] = [
        {
            "import_id": r[0],
            "decidido_em": r[1],
            "degrau": r[2],
            "env_sugerido": r[3],
            "env_escolhido": r[4],
        }
        for r in rows
    ]
    t["pendencias"] = roteamento_repo.listar_pendencias(limit=50)
    return t


class RegraConfigRequest(BaseModel):
    env_sp: str
    env_fora_sp: str
    ativa: bool


class ChaveRequest(BaseModel):
    tipo: Literal["raiz", "cnpj"]
    chave: str = Field(max_length=18)


class RedeRequest(BaseModel):
    nome: str
    env_slug: str
    chaves: list[ChaveRequest]


@router.get("/api/roteamento/regra")
def get_regra(_=Depends(require_user)):
    return {
        "config": regra_faturamento_repo.config(),
        "redes": regra_faturamento_repo.listar_redes(),
        "empresas": [
            {"slug": e["slug"], "name": e["name"]} for e in environments_repo.list_active()
        ],
    }


@router.put("/api/roteamento/regra")
def put_regra(payload: RegraConfigRequest, user: User = Depends(require_admin)):
    try:
        regra_faturamento_repo.set_config(
            env_sp=payload.env_sp,
            env_fora_sp=payload.env_fora_sp,
            ativa=payload.ativa,
            por=user.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"config": regra_faturamento_repo.config()}


def _salvar_rede(payload: RedeRequest, user: User, rede_id: int | None) -> dict:
    ativos = {e["slug"] for e in environments_repo.list_active()}
    if payload.env_slug not in ativos:
        raise HTTPException(
            status_code=400, detail=f"empresa {payload.env_slug!r} não é um ambiente ativo"
        )
    try:
        novo_id = regra_faturamento_repo.salvar_rede(
            nome=payload.nome,
            env_slug=payload.env_slug,
            chaves=[(c.tipo, c.chave) for c in payload.chaves],
            por=user.email,
            rede_id=rede_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": novo_id}


def _rede_existe(rede_id: int) -> bool:
    return any(r["id"] == rede_id for r in regra_faturamento_repo.listar_redes())


@router.post("/api/roteamento/regra/redes")
def post_rede(payload: RedeRequest, user: User = Depends(require_admin)):
    return _salvar_rede(payload, user, None)


@router.put("/api/roteamento/regra/redes/{rede_id}")
def put_rede(rede_id: int, payload: RedeRequest, user: User = Depends(require_admin)):
    if not _rede_existe(rede_id):
        raise HTTPException(status_code=404, detail="Rede não encontrada.")
    return _salvar_rede(payload, user, rede_id)


@router.delete("/api/roteamento/regra/redes/{rede_id}")
def delete_rede(rede_id: int, _=Depends(require_admin)):
    if not _rede_existe(rede_id):
        raise HTTPException(status_code=404, detail="Rede não encontrada.")
    regra_faturamento_repo.remover_rede(rede_id)
    return {"ok": True}


@router.get("/api/roteamento/decisoes")
def get_decisoes(import_id: str, _=Depends(require_user)):
    return roteamento_decisao_repo.listar_por_import(import_id)
