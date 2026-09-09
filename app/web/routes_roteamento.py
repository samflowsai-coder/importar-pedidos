"""Interruptor do roteamento e a evidência que autoriza virar a chave.

Três estados porque o problema é de sequência: não dá para validar o que não
está rodando, nem ligar o que não foi validado. `observando` roda o roteador e
grava o que ele teria feito, sem agir — e as **divergências** que saem daí são
o material de treinamento do time.

Quem vira a chave é admin. Não é configuração de operador.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.persistence import roteamento_repo
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
