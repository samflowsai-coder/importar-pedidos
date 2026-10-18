"""Portão único de preço antes do Fire e do XLSX.

`decidir_envio` é o ÚNICO ponto que diz se um pedido pode ser gravado no ERP
ou exportado. Combina o interruptor do ambiente (`preco_trava_modo`), a regra
de hoje (`check_order` + `is_blocking` + ack) e, em `ligado`, o avaliador puro
(`preco_avaliacao.avaliar`) com as aprovações locais.

- `desligado` (ou sem ambiente): bit a bit a regra de hoje.
- `observando`: regra de hoje + avaliação gravada em audit `preco_observado`;
  a avaliação nunca bloqueia e nada é criado para o Flow.
- `ligado`: a regra de hoje não decide; bloqueia por item não avaliado, item na
  fila sem decisão válida ou decisão recusada. Qualquer falha ao avaliar
  bloqueia (fail-closed): o portão grava direto no ERP de produção.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import partial
from typing import Any

import app.erp.product_check as pc
from app.erp.cnpj import cnpj_digits
from app.erp.preco_avaliacao import Avaliacao, ItemAvaliado, Motivo, Situacao, avaliar
from app.models.order import Order
from app.persistence import db, environments_repo, precos_repo, repo
from app.persistence import preco_aprovacao_repo as aprov

_APROVADAS = frozenset({"acordo", "regra", "preco_inicial", "auto_tabela"})

_MOTIVO_LEGIVEL: dict[str, str] = {
    "produto_nao_identificado": "produto não identificado no Fire",
    "cliente_sem_cnpj": "cliente sem CNPJ",
    "copia_desatualizada": "tabela de preço sem sincronizar com o Flow há mais de 24 h",
}


@dataclass
class DecisaoEnvio:
    bloqueado: bool
    reason: str | None = None
    detalhe: str | None = None
    block_detail: dict = field(default_factory=dict)
    metric_labels: list[str] = field(default_factory=list)
    precos_aplicar: dict[int, float] = field(default_factory=dict)
    avaliacao: Avaliacao | None = None


def _modo(env: dict | None) -> str:
    modo = (env or {}).get("preco_trava_modo")
    return modo if modo in ("observando", "ligado") else "desligado"


def cnpjs_grupo() -> set[str]:
    """Raízes (8 dígitos) de `cnpj` e `intercompany_cnpj` de TODOS os ambientes."""
    raizes: set[str] = set()
    for env in environments_repo.list_all():
        for campo in ("cnpj", "intercompany_cnpj"):
            d = cnpj_digits(env.get(campo))
            if len(d) >= 8:
                raizes.add(d[:8])
    return raizes


def _check_utilizavel(check: dict | None, order: Order) -> dict | None:
    """Check com identidade do produto: disponível e do mesmo tamanho do pedido."""
    if not check or not check.get("available"):
        return None
    if len(check.get("items") or []) != len(order.items):
        return None
    return check


def _todos(order: Order, check: dict | None, situacao: Situacao, motivo: Motivo) -> Avaliacao:
    itens = (check or {}).get("items") or []
    itens_av = []
    for i, it in enumerate(order.items):
        bruto = itens[i].get("fire_product_id") if i < len(itens) else None
        itens_av.append(
            ItemAvaliado(
                i,
                None if bruto is None else str(bruto),
                situacao,
                motivo,
                pc._to_cents(it.unit_price),
                None,
            )
        )
    return Avaliacao(rede=None, cnpj_rede=None, itens=itens_av)


def _avaliar_local(order: Order, check: dict | None, entry: dict, agora: datetime) -> Avaliacao:
    """Avalia contra a cópia local (db do ambiente ativo). Sem Fire, sem audit."""
    if check is None:
        return _todos(order, None, "nao_avaliado", "produto_nao_identificado")
    if entry.get("cliente_override_codigo") is not None:
        cnpj = cnpj_digits(entry.get("cliente_override_cnpj"))
        if len(cnpj) not in (8, 14):
            # Sem curto-circuito o avaliador cairia na raiz das lojas de entrega.
            return _todos(order, check, "nao_avaliado", "cliente_sem_cnpj")
    else:
        cnpj = order.header.customer_cnpj
    grupo = cnpjs_grupo()
    with db.connect() as conn:
        return avaliar(
            order,
            check,
            cnpj_cliente=cnpj,
            resolver_rede=partial(precos_repo.resolver_rede, conn),
            preco_vigente=partial(precos_repo.preco_vigente, conn),
            cnpjs_grupo=grupo,
            ultima_sync=precos_repo.ultima_sync_ok(conn),
            agora=agora,
        )


def avaliar_entry(
    entry: dict, order: Order, *, env: dict | None, agora: datetime
) -> Avaliacao | None:
    """Avaliação para o preview: só `entry["check"]` e a cópia local; sem I/O
    no Fire, sem audit e sem pendência. `desligado` → None."""
    if _modo(env) == "desligado":
        return None
    return _avaliar_local(order, _check_utilizavel(entry.get("check"), order), entry, agora)


def aplicar_precos(order: Order, precos: dict[int, float]) -> None:
    """Preço do aprovador nos itens sem preço (transiente, como o depara_apply)."""
    for i, preco in precos.items():
        order.items[i].unit_price = preco


def _regra_de_hoje(check: dict, ack_items: list) -> DecisaoEnvio:
    blocked, block_detail = pc.is_blocking(check, ack_items=ack_items)
    if not blocked:
        return DecisaoEnvio(False, block_detail=block_detail)
    labels: list[str] = []
    parts: list[str] = []
    if block_detail["items_mismatch"]:
        labels.append("price_mismatch")
        parts.append(f"{len(block_detail['items_mismatch'])} item(ns) com preço divergente do Fire")
    if block_detail["items_no_order_price"]:
        labels.append("missing_order_price")
        parts.append(f"{len(block_detail['items_no_order_price'])} item(ns) sem preço no pedido")
    if block_detail["items_no_price_unacked"]:
        labels.append("no_price_unacked")
        parts.append(
            f"{len(block_detail['items_no_price_unacked'])} item(ns) sem preço no cadastro "
            "do Fire (sem confirmação)"
        )
    return DecisaoEnvio(
        True,
        reason="price_check_failed",
        detalhe="Pedido bloqueado: " + "; ".join(parts) + ".",
        block_detail=block_detail,
        metric_labels=labels,
    )


def _bloqueio_trava(
    reason: str, detalhe: str, av: Avaliacao, decisoes: dict[int, dict[str, Any]]
) -> DecisaoEnvio:
    itens = [
        {
            "item_index": it.item_index,
            "fire_produto_id": it.fire_produto_id,
            "situacao": it.situacao,
            "motivo": it.motivo,
            "decisao": (decisoes.get(it.item_index) or {}).get("decisao"),
        }
        for it in av.itens
        if it.situacao != "ok" or it.item_index in decisoes
    ]
    return DecisaoEnvio(
        True,
        reason=reason,
        detalhe=detalhe,
        block_detail={"preco_trava": "ligado", "reason": reason, "itens": itens},
        metric_labels=[reason],
        avaliacao=av,
    )


def _decidir_ligado(
    import_id: str, order: Order, entry: dict, live: dict, ack_items: list, agora: datetime
) -> DecisaoEnvio:
    check = _check_utilizavel(live, order) or _check_utilizavel(entry.get("check"), order)
    vazio = Avaliacao(rede=None, cnpj_rede=None)
    try:
        if not order.items:
            return _bloqueio_trava(
                "preco_nao_avaliado",
                "0 item(ns) sem avaliação de preço: pedido sem itens.",
                vazio,
                {},
            )
        av = _avaliar_local(order, check, entry, agora)
        assinaturas = {
            it.item_index: aprov.assinatura(order.items[it.item_index], it.fire_produto_id)
            for it in av.itens
        }
        with db.connect() as conn:
            aprov.substituir_obsoletas(conn, import_id, assinaturas, em=agora)
            fila = [(it, assinaturas[it.item_index]) for it in av.pendentes]
            if fila:
                aprov.registrar_pendencias(conn, import_id, fila, agora=agora)
            decisoes = aprov.decisoes_validas(conn, import_id, assinaturas)
    except Exception as exc:  # noqa: BLE001 — fail-closed: sem avaliação não grava no ERP
        from app.utils.logger import logger

        logger.warning(f"trava de preço falhou ({type(exc).__name__}): {exc} — bloqueando")
        return _bloqueio_trava(
            "preco_nao_avaliado",
            f"{len(order.items)} item(ns) sem avaliação de preço: falha ao avaliar o preço.",
            vazio,
            {},
        )

    if any(it.situacao == "fora_intercompany" for it in av.itens):
        repo.append_audit(
            import_id,
            "preco_intercompany_fora_da_trava",
            {"cnpj_rede": av.cnpj_rede},
        )
        hoje = _regra_de_hoje(check, ack_items)
        hoje.avaliacao = av
        return hoje

    bloqueados = av.bloqueados
    recusados = [d for d in decisoes.values() if d.get("decisao") == "recusado"]
    aguardando: list[ItemAvaliado] = []
    precos: dict[int, float] = {}
    flow_ids: dict[int, Any] = {}
    for it in av.pendentes:
        d = decisoes.get(it.item_index)
        if d is None or d.get("decisao") not in _APROVADAS:
            if d is None or d.get("decisao") != "recusado":
                aguardando.append(it)
            continue
        if order.items[it.item_index].unit_price is None:
            if d.get("preco_final_centavos") is None:
                aguardando.append(it)
                continue
            precos[it.item_index] = int(d["preco_final_centavos"]) / 100
            flow_ids[it.item_index] = d.get("flow_id")

    partes: list[str] = []
    reason: str | None = None
    if bloqueados:
        motivos = ", ".join(
            dict.fromkeys(_MOTIVO_LEGIVEL.get(it.motivo, it.motivo) for it in bloqueados)
        )
        partes.append(f"{len(bloqueados)} item(ns) sem avaliação de preço: {motivos}.")
        reason = "preco_nao_avaliado"
    if recusados:
        motivos = "; ".join(
            dict.fromkeys((d.get("motivo_recusa") or "sem motivo informado") for d in recusados)
        )
        partes.append(f"Preço recusado no Flow: {motivos}.")
        reason = reason or "preco_recusado"
    if aguardando:
        partes.append(
            f"Pedido aguardando preço: {len(aguardando)} item(ns) na fila de aprovação "
            "do Flow (Rafael/Erica)."
        )
        reason = reason or "preco_aguardando"
    if reason is not None:
        return _bloqueio_trava(reason, " ".join(partes), av, decisoes)

    for i, preco in precos.items():
        repo.append_audit(
            import_id, "preco_aplicado", {"item_index": i, "preco": preco, "flow_id": flow_ids[i]}
        )
    return DecisaoEnvio(False, precos_aplicar=precos, avaliacao=av)


def decidir_envio(
    import_id: str, order: Order, entry: dict, *, env: dict | None, agora: datetime
) -> DecisaoEnvio:
    """Decide se o pedido pode ir ao Fire/XLSX. O chamador grava o audit de
    bloqueio (nome do seu caminho) com `block_detail` e conta `metric_labels`;
    ao liberar, chama `aplicar_precos(order, precos_aplicar)` antes de exportar.
    `agora` deve ter timezone."""
    modo = _modo(env)
    live = pc.check_order(order, env=env)
    ack_items = entry.get("sem_preco_ack_items") or []

    if modo == "ligado":
        return _decidir_ligado(import_id, order, entry, live, ack_items, agora)

    hoje = _regra_de_hoje(live, ack_items)
    if modo == "observando":
        try:
            check = _check_utilizavel(live, order) or _check_utilizavel(entry.get("check"), order)
            av = _avaliar_local(order, check, entry, agora)
            repo.append_audit(
                import_id,
                "preco_observado",
                {
                    "cnpj_rede": av.cnpj_rede,
                    "rede_id": av.rede.id if av.rede else None,
                    "regra_de_hoje_bloqueia": hoje.bloqueado,
                    "itens": [asdict(it) for it in av.itens],
                },
            )
            hoje.avaliacao = av
        except Exception as exc:  # noqa: BLE001 — observando nunca muda o envio
            from app.utils.logger import logger

            logger.warning(f"preco observando falhou ({type(exc).__name__}): {exc}")
    return hoje
