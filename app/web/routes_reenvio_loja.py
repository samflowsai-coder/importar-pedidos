"""Reenvio ao Flow, por loja, de um import que já foi COMBINADO (admin).

O envio novo já sai dividido por loja (`recebimento_por_loja`, interruptor por ambiente). Esta rota
cobre o que foi ao Flow antes disso: monta o recebimento por loja a partir do snapshot do import e
enfileira na fila normal do Portal (que tem o token e as janelas de envio). Só enfileira a loja cujo
pedido no Fire ainda está aberto: pedido faturado está finalizado e não vai ao Flow. Dry-run por
padrão (`aplicar=false`). Independe do `EXPORT_MODE`: é o caminho do Flow, não o do Fire.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app.erp import queries
from app.erp.cnpj import cnpj_digits
from app.erp.connection import FirebirdConnection
from app.erp.numero_pedido import variantes
from app.integrations.flowpcp import cliente_matriz, produto_erp, recebimento_por_loja
from app.integrations.flowpcp.client import FLOWPCP_TARGET_NAME, RECEBIMENTO_PATH
from app.integrations.flowpcp.intercompany import resolucao_para
from app.integrations.flowpcp.mapper import build_recebimentos_por_loja
from app.models.order import Order
from app.persistence import context as env_context
from app.persistence import environments_repo, outbox_repo, repo
from app.utils.logger import logger
from app.web.auth import User, require_admin

router = APIRouter()

_JANELA_DIAS = 90
_ENCERRADOS = ("FATURADO", "CANCELADO")
_TAMANHO_PEDIDO_CLIENTE = 20


def _ler_pedidos_do_fire(slug: str, numero: str) -> list[tuple]:
    """(PEDIDO_CLIENTE, V.CODIGO, V.STATUS, V.DATA_PEDIDO, C.CODIGO, C.CPF_CNPJ). Só SELECT."""
    env = environments_repo.get_by_slug(slug)
    chaves = [v for v in variantes(numero) if len(v) <= _TAMANHO_PEDIDO_CLIENTE]
    if env is None or not chaves:
        return []
    with FirebirdConnection().connect_somente_leitura(environments_repo.to_fb_config(env)) as fb:
        cur = fb.cursor()
        try:
            cur.execute(queries.FIND_ORDERS_BY_PEDIDO_CLIENTE(len(chaves)), chaves)
            return list(cur.fetchall())
        finally:
            cur.close()


def _data(valor) -> date | None:
    if isinstance(valor, date):
        return valor
    texto = str(valor or "")[:10]
    try:
        return date.fromisoformat(texto)
    except ValueError:
        return None


def planejar(lojas: list[str], linhas: list[tuple], data_ref: date | None) -> list[dict]:
    """Para cada loja (CNPJ de 14 dígitos): o pedido do Fire e se vale enfileirar. Pedido
    cancelado não conta; entre os demais vale o de DATA_PEDIDO mais próxima do import (e dentro de
    90 dias dele, quando as duas datas existem). Aberto = fora de FATURADO/CANCELADO."""
    plano = []
    for cnpj in lojas:
        candidatas = []
        for linha in linhas:
            status = str(linha[2] or "").strip().upper()
            if cnpj_digits(linha[5]) != cnpj or status == "CANCELADO":
                continue
            dia = _data(linha[3])
            if data_ref and dia and abs((data_ref - dia).days) > _JANELA_DIAS:
                continue
            distancia = abs((data_ref - dia).days) if data_ref and dia else 10**9
            candidatas.append((distancia, int(linha[1]), status))
        if not candidatas:
            plano.append(
                {
                    "cnpj": cnpj,
                    "pedido_fire": None,
                    "estado": None,
                    "enfileirar": False,
                    "motivo": "sem_pedido_no_fire",
                }
            )
            continue
        _, codigo, status = min(candidatas)
        aberto = status not in _ENCERRADOS
        plano.append(
            {
                "cnpj": cnpj,
                "pedido_fire": codigo,
                "estado": status,
                "enfileirar": aberto,
                "motivo": "" if aberto else status.lower(),
            }
        )
    return plano


@router.post("/api/imported/{import_id}/reenviar-flow-por-loja")
def reenviar_flow_por_loja(
    import_id: str,
    aplicar: bool = Query(default=False),
    admin: User = Depends(require_admin),
) -> dict:
    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="import não encontrado")
    if not entry.get("fire_codigo"):
        raise HTTPException(
            status_code=409, detail="import sem fire_codigo: o pedido ainda não consta no Fire"
        )
    try:
        order = Order.model_validate(entry.get("snapshot") or {})
    except Exception:  # noqa: BLE001 — snapshot ilegível é 409, não 500
        raise HTTPException(status_code=409, detail="snapshot do import ilegível") from None

    slug = env_context.current_env_slug()
    resolucao = resolucao_para(order, slug=slug)
    if not recebimento_por_loja.deve_dividir(order, resolucao):
        raise HTTPException(
            status_code=409,
            detail="este import não divide por loja (header com CNPJ, item sem loja de 14 dígitos "
            "ou intercompany resolvido)",
        )
    if cliente_matriz.para_pedido(slug, order, resolucao) is not None:
        raise HTTPException(
            status_code=409,
            detail="este pedido vai ao Flow como pedido único da matriz "
            "(PORTAL_FLOWPCP_CLIENTE_MATRIZ_*): não se reenvia por loja",
        )
    if aplicar and not recebimento_por_loja.ligado(slug):
        raise HTTPException(
            status_code=409,
            detail="PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA está desligado para este ambiente",
        )

    produto_erp.anexar_produto_erp(
        order, produto_erp.seqs_por_item(len(order.items), check=entry.get("check"))
    )
    lojas = recebimento_por_loja.lojas(order)
    linhas = _ler_pedidos_do_fire(slug, order.header.order_number or "")
    plano = planejar(list(lojas), linhas, _data(entry.get("imported_at")))
    for p in plano:
        p["indices"] = lojas[p["cnpj"]]
        p["itens"] = len(lojas[p["cnpj"]])

    estado_codigo = next(
        (
            str(linha[2] or "").strip().upper()
            for linha in linhas
            if int(linha[1]) == entry["fire_codigo"]
        ),
        None,
    )
    if aplicar and estado_codigo in _ENCERRADOS:
        raise HTTPException(
            status_code=409,
            detail=f"o fire_codigo do import ({entry['fire_codigo']}) está {estado_codigo}: a fila "
            "descartaria também as linhas das lojas abertas. Corrija o ponteiro antes de reenviar.",
        )
    enfileiradas = 0
    if aplicar:
        reqs = {
            r.cliente.cnpj: r
            for r in build_recebimentos_por_loja(
                import_id=import_id, order=order, tenant_id="", resolucao=resolucao
            )
        }
        for p in plano:
            if not p["enfileirar"]:
                continue
            req = reqs[p["cnpj"]]
            try:
                outbox_repo.enqueue(
                    import_id=import_id,
                    target=FLOWPCP_TARGET_NAME,
                    endpoint=RECEBIMENTO_PATH,
                    payload=req.model_dump(by_alias=True),
                    idempotency_key=f"send-{import_id}-{p['cnpj']}",
                )
                enfileiradas += 1
            except outbox_repo.OutboxDuplicateError:
                p["motivo"] = "ja_enfileirada"
        repo.append_audit(
            import_id,
            "flowpcp_reenvio_por_loja",
            {
                "por": admin.email,
                "enfileiradas": [p["cnpj"] for p in plano if p["enfileirar"] and p["motivo"] == ""],
                "puladas": {p["cnpj"]: p["motivo"] for p in plano if p["motivo"]},
            },
        )
        logger.info(f"reenvio por loja: {import_id} -> {enfileiradas} linha(s) por {admin.email}")
    return {
        "import_id": import_id,
        "aplicado": aplicar,
        "fire_codigo": entry["fire_codigo"],
        "fire_codigo_estado": estado_codigo,
        "lojas": plano,
        "enfileiradas": enfileiradas,
    }
