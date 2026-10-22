"""Vínculo manual de um import `parsed` ao pedido do Fire (admin).

A reconciliação casa por número do pedido + cliente. Quando o número que o Portal leu não é o
`PEDIDO_CLIENTE` que o Fire guarda (ex.: "TENNIS STATION LJ 180 181" × "TSLJ180181"), o import
fica `parsed` para sempre e o envio ao Flow, `retido`. Aqui o admin diz qual pedido do Fire é.

Segurança: o pedido TEM que existir no Fire e o CNPJ dele tem que ser o do header ou de uma loja de
entrega do import (ou o cliente escolhido no override). Só liga import `parsed`: o `UPDATE` é o
mesmo compare-and-set da reconciliação (`repo.mark_found_in_fire`).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.erp.cnpj import cnpj_digits
from app.erp.connection import FirebirdConnection
from app.erp_eventos.leitura import LeitorFirebird
from app.erp_eventos.modelo import PedidoErp
from app.persistence import context as env_context
from app.persistence import environments_repo, repo
from app.utils.logger import logger
from app.web.auth import User, require_admin

router = APIRouter()

# `caminho_match` do evento: 1-3 são os da reconciliação; 4 = vínculo manual.
CAMINHO_MANUAL = 4


class VincularFireRequest(BaseModel):
    fire_codigo: int = Field(gt=0)


def _ler_pedido_do_fire(slug: str, codigo: int) -> PedidoErp | None:
    """Só SELECT, na transação READ ONLY do SP1."""
    env = environments_repo.get_by_slug(slug)
    if env is None:
        return None
    with FirebirdConnection().connect_somente_leitura(environments_repo.to_fb_config(env)) as fb:
        return LeitorFirebird(fb).por_codigo([codigo]).get(codigo)


def _ancoras(entry: dict) -> tuple[set[str], int | None]:
    """CNPJs (dígitos) que o import aceita como cliente do pedido, e o override, se houver."""
    cnpjs = {cnpj_digits(entry.get("customer_cnpj"))}
    snapshot = entry.get("snapshot") or {}
    for item in snapshot.get("items") or []:
        cnpjs.add(cnpj_digits(item.get("delivery_cnpj")))
    cnpjs.discard("")
    return cnpjs, entry.get("cliente_override_codigo")


@router.post("/api/imported/{import_id}/vincular-fire")
def vincular_fire(
    import_id: str, body: VincularFireRequest, admin: User = Depends(require_admin)
) -> dict:
    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="import não encontrado")
    if entry.get("portal_status") != "parsed":
        raise HTTPException(
            status_code=409,
            detail=f"import já está '{entry.get('portal_status')}'; só 'parsed' pode ser vinculado",
        )

    slug = env_context.current_env_slug()
    pedido = _ler_pedido_do_fire(slug, body.fire_codigo)
    if pedido is None:
        raise HTTPException(status_code=404, detail="pedido não existe no Fire deste ambiente")

    cnpjs, override = _ancoras(entry)
    cnpj_fire = cnpj_digits(pedido.cliente_erp_cnpj)
    if not (
        (override is not None and override == pedido.cliente_erp_codigo)
        or (cnpj_fire and cnpj_fire in cnpjs)
    ):
        raise HTTPException(
            status_code=409,
            detail="o cliente do pedido no Fire não é o do import (CNPJ do header, das lojas ou override)",
        )

    agora = datetime.now(UTC).isoformat(timespec="seconds")
    venceu = repo.mark_found_in_fire(
        import_id,
        fire_codigo=body.fire_codigo,
        fire_status=pedido.status,
        caminho=CAMINHO_MANUAL,
        lojas_casadas=0,
        at=agora,
    )
    if not venceu:
        raise HTTPException(status_code=409, detail="outro gatilho vinculou este import agora")
    repo.append_audit(
        import_id,
        "vinculo_fire_manual",
        {"fire_codigo": body.fire_codigo, "fire_status": pedido.status, "por": admin.email},
    )
    logger.info(f"vinculo_fire_manual: {import_id} -> {body.fire_codigo} por {admin.email}")
    return {"import_id": import_id, "fire_codigo": body.fire_codigo, "fire_status": pedido.status}
