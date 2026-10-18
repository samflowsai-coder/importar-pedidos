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
from datetime import date, datetime
from typing import Any

import app.erp.product_check as pc
from app.erp import preco_regra
from app.erp.preco_avaliacao import (
    Avaliacao,
    ItemAvaliado,
    Motivo,
    Situacao,
    avaliar,
    cnpj_da_rede,
    data_ref_do_pedido,
    unidade_normalizada,
)
from app.models.order import Order
from app.persistence import db, environments_repo, precos_repo, repo
from app.persistence import preco_aprovacao_repo as aprov

_APROVADAS = frozenset({"acordo", "regra", "preco_inicial", "auto_tabela"})

_MOTIVO_LEGIVEL: dict[str, str] = {
    "produto_nao_identificado": "produto não identificado no Fire",
    "cliente_sem_cnpj": "cliente sem CNPJ",
    "cliente_sem_rede": "cliente sem rede na tabela de preço do Flow",
    "unidade_divergente": (
        "unidade do produto no Fire não confere com a tabela de preço (diferente ou não informada)"
    ),
    "preco_invalido": "preço da tabela fora do contrato",
    "copia_desatualizada": "tabela de preço sem sincronizar com o Flow há mais de 24 h",
    "empresa_sem_cnpj": "CNPJ da empresa não cadastrado no ambiente",
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
    # {item_index, preco, flow_id} por preço aplicado; o chamador audita
    # `preco_aplicado` só depois que o pedido saiu (registrar_aplicados).
    aplicados: list[dict] = field(default_factory=list)


def _modo(env: dict | None) -> str:
    modo = (env or {}).get("preco_trava_modo")
    return modo if modo in ("observando", "ligado") else "desligado"


def trava_ativa(env: dict | None) -> bool:
    """Trava em `observando` ou `ligado` (XLSX só pelo portão)."""
    return _modo(env) != "desligado"


def cnpjs_grupo() -> set[str]:
    """Raízes (8 caracteres, texto) de `cnpj` e `intercompany_cnpj` de TODOS os
    ambientes, pelo `preco_regra.normalizar_cnpj` (CNPJ fora do contrato não entra)."""
    raizes: set[str] = set()
    for env in environments_repo.list_all():
        for campo in ("cnpj", "intercompany_cnpj"):
            bruto = env.get(campo)
            cnpj = preco_regra.normalizar_cnpj(bruto)
            if cnpj is not None:
                raizes.add(cnpj[:8])
            elif bruto:
                from app.utils.logger import logger

                # Sem o valor no log: só o campo e o ambiente.
                logger.warning(
                    f"cnpjs_grupo: {campo} do ambiente {env.get('slug')!r} fora do formato "
                    "do contrato; fica fora das empresas do grupo"
                )
    return raizes


def _check_utilizavel(check: dict | None, order: Order) -> dict | None:
    """Check com identidade do produto: disponível e do mesmo tamanho do pedido."""
    if not check or not check.get("available"):
        return None
    if len(check.get("items") or []) != len(order.items):
        return None
    return check


def _com_vinculos(check: dict | None, produtos_por_item: dict[int, int] | None) -> dict | None:
    """Check com o SEQ do vínculo de-para nos itens vinculados: o Fire recebe
    esse SEQ (`FirebirdExporter(produtos_por_item=...)`) e o XLSX o código
    dele, então é ele que a trava avalia, mesmo quando o item também casa
    direto por EAN/código com outro produto. Nesse caso a `fire_unidade` do
    check é a do OUTRO produto: sai (unidade desconhecida bloqueia)."""
    if check is None or not produtos_por_item:
        return check
    itens = [dict(it) for it in check.get("items") or []]
    for i, seq in produtos_por_item.items():
        if 0 <= i < len(itens):
            if str(itens[i].get("fire_product_id")) != str(seq):
                itens[i]["fire_unidade"] = None
            itens[i]["fire_product_id"] = seq
    return {**check, "items": itens}


def _todos(order: Order, check: dict | None, situacao: Situacao, motivo: Motivo) -> Avaliacao:
    itens = (check or {}).get("items") or []
    itens_av = []
    for i, it in enumerate(order.items):
        bruto = itens[i].get("fire_product_id") if i < len(itens) else None
        unidade = itens[i].get("fire_unidade") if i < len(itens) else None
        itens_av.append(
            ItemAvaliado(
                item_index=i,
                fire_produto_id=None if bruto is None else str(bruto),
                situacao=situacao,
                motivo=motivo,
                preco_pedido_centavos=preco_regra.centavos(it.unit_price),
                preco_tabela_centavos=None,
                preco_tabela_id=None,
                unidade_erp=unidade_normalizada(unidade),
            )
        )
    return Avaliacao(rede=None, cnpj_rede=None, itens=itens_av)


def _cnpj_cliente(order: Order, entry: dict) -> str | None:
    """CNPJ do cliente: o do override quando há override (None se ele não tem
    CNPJ válido), senão o do cabeçalho."""
    if entry.get("cliente_override_codigo") is not None:
        return preco_regra.normalizar_cnpj(entry.get("cliente_override_cnpj"))
    return order.header.customer_cnpj


def _eh_intercompany(order: Order, entry: dict) -> bool:
    if entry.get("cliente_override_codigo") is not None and _cnpj_cliente(order, entry) is None:
        return False
    rede = cnpj_da_rede(order, _cnpj_cliente(order, entry))
    return rede is not None and rede[:8] in cnpjs_grupo()


def _avaliar_local(
    order: Order, check: dict | None, entry: dict, agora: datetime, env: dict | None
) -> Avaliacao:
    """Avalia contra a cópia local (db do ambiente ativo). Sem Fire, sem audit.
    A empresa faturadora é o CNPJ do ambiente; sem ele, tudo `nao_avaliado`."""
    if check is None:
        return _todos(order, None, "nao_avaliado", "produto_nao_identificado")
    cnpj = _cnpj_cliente(order, entry)
    if entry.get("cliente_override_codigo") is not None and cnpj is None:
        # Sem curto-circuito o avaliador cairia na raiz das lojas de entrega.
        return _todos(order, check, "nao_avaliado", "cliente_sem_cnpj")
    empresa = preco_regra.normalizar_cnpj((env or {}).get("cnpj"))
    if empresa is None:
        # Sem empresa faturadora nenhum preço é da empresa: bloqueia, sem fila.
        return _todos(order, check, "nao_avaliado", "empresa_sem_cnpj")
    grupo = cnpjs_grupo()
    with db.connect() as conn:

        def _rede(cnpj_rede: str):
            normalizado = preco_regra.normalizar_cnpj(cnpj_rede)
            return precos_repo.resolver_rede(conn, normalizado) if normalizado else None

        def _preco(rede_id: str | None, produto: str, data: date) -> preco_regra.PrecoLinha | None:
            return precos_repo.preco_vigente(
                conn, empresa_cnpj=empresa, rede_id=rede_id, erp_produto_id=produto, data=data
            )

        return avaliar(
            order,
            check,
            cnpj_cliente=cnpj,
            resolver_rede=_rede,
            preco_vigente=_preco,
            cnpjs_grupo=grupo,
            ultima_sync=precos_repo.ultima_sync_ok(conn),
            agora=agora,
            data_ref=data_ref_do_pedido(order, entry.get("imported_at"), agora),
        )


def avaliar_entry(
    entry: dict,
    order: Order,
    *,
    env: dict | None,
    agora: datetime,
    produtos_por_item: dict[int, int] | None = None,
) -> Avaliacao | None:
    """Avaliação para o preview: só `entry["check"]` e a cópia local; sem I/O
    no Fire, sem audit e sem pendência. `desligado` → None.
    `produtos_por_item`: SEQ do vínculo de-para por item (o mesmo do envio)."""
    if _modo(env) == "desligado":
        return None
    check = _com_vinculos(_check_utilizavel(entry.get("check"), order), produtos_por_item)
    return _avaliar_local(order, check, entry, agora, env)


def bloqueio_do_pedido(order: Order, entry: dict, check: dict | None) -> str | None:
    """Bloqueio de nível de pedido (antes de olhar item): texto do portão ou None."""
    if not order.items:
        return "Pedido sem itens: nada para avaliar."
    if check is None and _eh_intercompany(order, entry):
        return "Pedido intercompany: não foi possível checar no Fire agora; tente de novo."
    return None


def classificar_itens(
    order: Order, av: Avaliacao, decisoes: dict[int, dict[str, Any]]
) -> tuple[dict[int, str], dict[int, float], dict[int, Any]]:
    """Estado final de cada item — a ÚNICA fonte do portão e do preview.

    Estados: ok | aguardando | aprovado | recusado | nao_avaliado | intercompany.
    Também devolve os preços do aprovador a aplicar (item sem preço no pedido)
    e o flow_id de cada um."""
    estados: dict[int, str] = {}
    precos: dict[int, float] = {}
    flow_ids: dict[int, Any] = {}
    for it in av.itens:
        i = it.item_index
        d = decisoes.get(i)
        if it.situacao == "fora_intercompany":
            estados[i] = "intercompany"
        elif it.situacao == "nao_avaliado":
            estados[i] = "nao_avaliado"
        elif d is not None and d.get("decisao") == "recusado":
            estados[i] = "recusado"
        elif it.situacao == "ok":
            estados[i] = "ok"
        elif d is None or d.get("decisao") not in _APROVADAS:
            estados[i] = "aguardando"
        elif order.items[i].unit_price is None:
            if d.get("preco_final_centavos") is None:
                estados[i] = "aguardando"
            else:
                estados[i] = "aprovado"
                precos[i] = int(d["preco_final_centavos"]) / 100
                flow_ids[i] = d.get("flow_id")
        else:
            estados[i] = "aprovado"
    return estados, precos, flow_ids


def assinaturas_da_avaliacao(order: Order, av: Avaliacao) -> dict[int, str]:
    """Assinatura atual de cada item avaliado (chave das decisões do Flow):
    a rede resolvida quando há, senão a raiz do CNPJ."""
    rede_id = av.rede.id if av.rede is not None else None
    return {
        it.item_index: aprov.assinatura(
            order.items[it.item_index], it.fire_produto_id, av.cnpj_rede, rede_id=rede_id
        )
        for it in av.itens
    }


def _reais(centavos: int | None) -> float | None:
    return None if centavos is None else round(centavos / 100, 2)


def preview_preco(
    import_id: str,
    entry: dict,
    order: Order,
    *,
    env: dict | None,
    agora: datetime,
    produtos_por_item: dict[int, int] | None = None,
) -> dict | None:
    """Bloco `preco` do preview: estado por item, igual ao que o portão decidiria
    (mesmo `classificar_itens`). Só leitura (sem Fire, sem audit, sem pendência).
    `desligado` → None."""
    check = _check_utilizavel(entry.get("check"), order)
    av = avaliar_entry(entry, order, env=env, agora=agora, produtos_por_item=produtos_por_item)
    if av is None:
        return None
    with db.connect() as conn:
        decisoes = aprov.decisoes_validas(conn, import_id, assinaturas_da_avaliacao(order, av))
    estados, _, _ = classificar_itens(order, av, decisoes)
    bloqueio = bloqueio_do_pedido(order, entry, check)
    if bloqueio is not None:
        estados = dict.fromkeys(estados, "nao_avaliado")
    resumo = {
        "ok": 0,
        "fila": 0,
        "aprovados": 0,
        "recusados": 0,
        "nao_avaliados": 0,
        "intercompany": 0,
    }
    chave = {
        "ok": "ok",
        "aguardando": "fila",
        "aprovado": "aprovados",
        "recusado": "recusados",
        "nao_avaliado": "nao_avaliados",
        "intercompany": "intercompany",
    }
    itens = []
    for it in av.itens:
        estado = estados[it.item_index]
        resumo[chave[estado]] += 1
        itens.append(
            {
                "item_index": it.item_index,
                "situacao": it.situacao,
                "motivo": it.motivo,
                "preco_pedido": _reais(it.preco_pedido_centavos),
                "preco_tabela": _reais(it.preco_tabela_centavos),
                "decisao": (decisoes.get(it.item_index) or {}).get("decisao"),
                "estado": estado,
            }
        )
    return {"modo": _modo(env), "itens": itens, "resumo": resumo, "bloqueio_pedido": bloqueio}


def aplicar_precos(order: Order, precos: dict[int, float]) -> None:
    """Preço do aprovador nos itens sem preço (transiente, como o depara_apply)."""
    for i, preco in precos.items():
        order.items[i].unit_price = preco


def registrar_aplicados(import_id: str, decisao: DecisaoEnvio) -> None:
    """Audit `preco_aplicado`, um por item. Chamar só depois que o pedido saiu
    (insert no Fire ou XLSX gerado com sucesso)."""
    for aplicado in decisao.aplicados:
        repo.append_audit(import_id, "preco_aplicado", aplicado)


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
    import_id: str,
    order: Order,
    entry: dict,
    live: dict,
    ack_items: list,
    agora: datetime,
    produtos_por_item: dict[int, int] | None,
    env: dict | None,
) -> DecisaoEnvio:
    check = _com_vinculos(
        _check_utilizavel(live, order) or _check_utilizavel(entry.get("check"), order),
        produtos_por_item,
    )
    vazio = Avaliacao(rede=None, cnpj_rede=None)
    try:
        bloqueio = bloqueio_do_pedido(order, entry, check)
        if bloqueio is not None:
            return _bloqueio_trava("preco_nao_avaliado", bloqueio, vazio, {})
        if check is None:
            # Fire fora e sem check salvo: sem produto não há assinatura. Bloqueia
            # ANTES de tocar em preco_aprovacao — senão substituir_obsoletas
            # cancelaria toda decisão já dada e ela se perderia na volta do Fire.
            av = _todos(order, None, "nao_avaliado", "produto_nao_identificado")
            return _bloqueio_trava(
                "preco_nao_avaliado",
                f"{len(order.items)} item(ns) sem avaliação de preço: "
                f"{_MOTIVO_LEGIVEL['produto_nao_identificado']}.",
                av,
                {},
            )
        av = _avaliar_local(order, check, entry, agora, env)
        assinaturas = assinaturas_da_avaliacao(order, av)
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

    estados, precos, flow_ids = classificar_itens(order, av, decisoes)
    bloqueados = [it for it in av.itens if estados[it.item_index] == "nao_avaliado"]
    recusados = [decisoes[i] for i, e in estados.items() if e == "recusado"]
    aguardando = [it for it in av.itens if estados[it.item_index] == "aguardando"]

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
        partes.append(f"Pedido aguardando aprovação de preço no Flow: {len(aguardando)} item(ns).")
        reason = reason or "preco_aguardando"
    if reason is not None:
        return _bloqueio_trava(reason, " ".join(partes), av, decisoes)

    aplicados = [{"item_index": i, "preco": p, "flow_id": flow_ids[i]} for i, p in precos.items()]
    return DecisaoEnvio(False, precos_aplicar=precos, avaliacao=av, aplicados=aplicados)


def decidir_envio(
    import_id: str,
    order: Order,
    entry: dict,
    *,
    env: dict | None,
    agora: datetime,
    produtos_por_item: dict[int, int] | None = None,
) -> DecisaoEnvio:
    """Decide se o pedido pode ir ao Fire/XLSX. O chamador grava o audit de
    bloqueio (nome do seu caminho) com `block_detail` e conta `metric_labels`;
    ao liberar, chama `aplicar_precos(order, precos_aplicar)` antes de exportar.
    `agora` deve ter timezone.

    `produtos_por_item`: {posição: SEQ} dos vínculos de-para, o MESMO mapa que
    vai ao `FirebirdExporter` (e cujos códigos vão ao XLSX). Em `observando` e
    `ligado` a avaliação usa esse SEQ; `desligado` não olha (regra de hoje)."""
    modo = _modo(env)
    live = pc.check_order(order, env=env)
    ack_items = entry.get("sem_preco_ack_items") or []

    if modo == "ligado":
        return _decidir_ligado(
            import_id, order, entry, live, ack_items, agora, produtos_por_item, env
        )

    hoje = _regra_de_hoje(live, ack_items)
    if modo == "observando":
        try:
            check = _com_vinculos(
                _check_utilizavel(live, order) or _check_utilizavel(entry.get("check"), order),
                produtos_por_item,
            )
            av = _avaliar_local(order, check, entry, agora, env)
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
