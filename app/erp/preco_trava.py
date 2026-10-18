"""Portão único de preço antes do Fire e do XLSX.

`decidir_envio` é o ÚNICO ponto que diz se um pedido pode ser gravado no ERP
ou exportado. Combina o interruptor do ambiente (`preco_trava_modo`), a regra
de hoje (`check_order` + `is_blocking` + ack) e, em `ligado`, o avaliador puro
(`preco_avaliacao.avaliar`) com as aprovações locais.

- `desligado` (ou sem ambiente): bit a bit a regra de hoje.
- `observando`: regra de hoje + avaliação gravada em audit `preco_observado`;
  a avaliação nunca bloqueia e nada é criado para o Flow.
- `ligado`: a regra de hoje não decide. Pedido 100% ok contra a cópia segue
  sem falar com o Flow. Com item na fila, o portão garante a revisão do
  pedido (`preco_revisao_repo`, contrato precos v1 §4) e só libera quando
  toda `itemChave` da revisão vigente tem decisão do Flow e nenhuma é
  `recusado` (§1 passo 6); o preço que vai ao ERP é o `precoAprovadoCentavos`
  de cada decisão. Qualquer falha ao avaliar bloqueia (fail-closed): o portão
  grava direto no ERP de produção.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import app.erp.product_check as pc
from app.erp import preco_regra
from app.erp.mapper import parse_date
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
from app.integrations.flowpcp import precos_schema
from app.models.order import Order
from app.persistence import db, environments_repo, precos_repo, repo
from app.persistence import preco_revisao_repo as revs

MOTIVO_RETIRADA_LOCAL = "pedido liberado pela tabela de preço no Portal"
MOTIVO_RETIRADA_INTERCOMPANY = "pedido intercompany, fora da trava de preço"

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
                itens[i]["fire_description"] = None
            itens[i]["fire_product_id"] = seq
    return {**check, "items": itens}


def _fonte(env: dict | None) -> str | None:
    """Slug do ambiente que dá a chave do produto (`preco_fonte_slug`) quando
    não é o próprio: a Nasmar avalia pelo SEQ da MM (contrato §9.2)."""
    slug = ((env or {}).get("preco_fonte_slug") or "").strip().lower()
    return slug if slug and slug != (env or {}).get("slug") else None


def _check_da_fonte(order: Order, fonte: str) -> dict | None:
    """`check_order` no Fire do ambiente fonte, com o contexto DELE ativo: o
    degrau de-para lê o SQLite da fonte, nunca o do ambiente do pedido.
    Fonte inexistente → None (bloqueia)."""
    from app.persistence import context as env_context

    amb = environments_repo.get_by_slug(fonte)
    if amb is None:
        return None
    with env_context.active_env(amb["id"], amb["slug"]):
        return pc.check_order(order, env=amb)


def _check_para_preco(
    order: Order,
    entry: dict,
    live: dict | None,
    env: dict | None,
    produtos_por_item: dict[int, int] | None = None,
) -> dict | None:
    """Check cujo `fire_product_id`/`fire_unidade` a trava avalia e manda no corpo.
    Sem fonte (ou fonte = o próprio) ou pedido intercompany (fora da trava, não
    depende da fonte): o check de hoje com os vínculos de-para.
    Com fonte: o check do Fire da fonte, sem fallback para check salvo (fonte
    fora → bloqueia) e SEM os vínculos do ambiente, que são SEQs do Fire dele."""
    fonte = _fonte(env)
    if fonte is None or _eh_intercompany(order, entry):
        return _com_vinculos(
            _check_utilizavel(live, order) or _check_utilizavel(entry.get("check"), order),
            produtos_por_item,
        )
    return _check_utilizavel(_check_da_fonte(order, fonte), order)


def _check_salvo_para_preco(
    entry: dict, order: Order, env: dict | None, produtos_por_item: dict[int, int] | None
) -> dict | None:
    """O mesmo do `_check_para_preco` para o preview, só com o que está salvo:
    com fonte, `entry["check_preco"]` (gravado no commit e no vínculo) — e só
    se foi gerado por essa mesma fonte; de outra fonte = sem avaliação."""
    fonte = _fonte(env)
    if fonte is None or _eh_intercompany(order, entry):
        return _com_vinculos(_check_utilizavel(entry.get("check"), order), produtos_por_item)
    salvo = entry.get("check_preco")
    if not salvo or salvo.get("preco_fonte_slug") != fonte:
        return None
    return _check_utilizavel(salvo, order)


def check_preco_para_gravar(
    order: Order, env: dict | None, entry: dict | None = None
) -> dict | None:
    """Check do ambiente fonte para gravar em `check_preco` (commit e vínculo
    de-para), marcado com a fonte que o gerou (`preco_fonte_slug`). None quando
    a trava está desligada, quando não há fonte, quando o pedido é intercompany
    (nenhuma consulta extra) ou quando o Fire da fonte não respondeu."""
    fonte = _fonte(env)
    if _modo(env) == "desligado" or fonte is None:
        return None
    try:
        if _eh_intercompany(order, entry or {}):
            return None
        check = _check_da_fonte(order, fonte)
    except Exception as exc:  # noqa: BLE001 — o preview fica sem avaliação; o portão refaz
        from app.utils.logger import logger

        logger.warning(f"check_preco da fonte {fonte!r} falhou ({type(exc).__name__}): {exc}")
        return None
    if not check or not check.get("available"):
        return None
    return {**check, "preco_fonte_slug": fonte}


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
    check = _check_salvo_para_preco(entry, order, env, produtos_por_item)
    return _avaliar_local(order, check, entry, agora, env)


def bloqueio_do_pedido(order: Order, entry: dict, check: dict | None) -> str | None:
    """Bloqueio de nível de pedido (antes de olhar item): texto do portão ou None."""
    if not order.items:
        return "Pedido sem itens: nada para avaliar."
    if check is None and _eh_intercompany(order, entry):
        return "Pedido intercompany: não foi possível checar no Fire agora; tente de novo."
    return None


def item_chave(import_id: str, item_index: int) -> str:
    """Identidade estável da linha no contrato (`itemChave`)."""
    return f"{import_id}:{item_index}"


def _quantidade_2(q: float | None) -> Decimal | None:
    """Quantidade a 2 casas (`Decimal.quantize`, decisão 3); None se ausente."""
    if q is None or not math.isfinite(q):
        return None
    return Decimal(str(q)).quantize(Decimal("0.01"))


def conteudo_hash(
    order: Order, av: Avaliacao, *, cliente_cnpj: str | None, empresa_cnpj: str
) -> str:
    """`sha256:` do JSON canônico do que muda a avaliação (contrato §4.2):
    cliente, empresa e, por item, produto, unidade, quantidade e preço. A
    descrição não entra. CNPJs já normalizados (estável entre deploys)."""
    itens = []
    for it in av.itens:
        q = _quantidade_2(order.items[it.item_index].quantity)
        itens.append(
            {
                "linha": it.item_index,
                "erpProdutoId": it.fire_produto_id,
                "unidadeErp": it.unidade_erp,
                "quantidade": None if q is None else str(q),
                "precoPedidoCentavos": it.preco_pedido_centavos,
            }
        )
    base = {"cliente_cnpj": cliente_cnpj, "empresa_cnpj": empresa_cnpj, "itens": itens}
    texto = json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _instante_z(dt: datetime) -> str:
    """Instante como o `toISOString()` do JS (UTC, milissegundos, `Z`)."""
    u = dt.astimezone(UTC)
    return u.strftime("%Y-%m-%dT%H:%M:%S.") + f"{u.microsecond // 1000:03d}Z"


def emitido_em_do_pedido(order: Order) -> str | None:
    """`issue_date` à 00:00 em São Paulo, em UTC (decisão 2); sem emissão, None."""
    d = parse_date(order.header.issue_date)
    if d is None:
        return None
    return _instante_z(datetime(d.year, d.month, d.day, tzinfo=preco_regra.TZ))


def recebido_em_do_pedido(recebido_em: str | datetime | None, agora: datetime) -> str:
    """`imported_at` (sem fuso = hora de São Paulo) com fuso; sem ele, `agora`."""
    dt: datetime | None = recebido_em if isinstance(recebido_em, datetime) else None
    if isinstance(recebido_em, str) and recebido_em.strip():
        try:
            dt = datetime.fromisoformat(recebido_em.strip())
        except ValueError:
            dt = None
    if dt is None:
        dt = agora
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=preco_regra.TZ)
    return _instante_z(dt)


def _utf16(texto: str) -> int:
    return len(texto.encode("utf-16-le")) // 2


def _texto(*candidatos: object, maximo: int) -> str | None:
    """Primeiro texto não vazio, truncado a `maximo` unidades UTF-16 (como o
    `.max()` do zod), sem partir um caractere."""
    for c in candidatos:
        if isinstance(c, str) and c.strip():
            t = c.strip()[:maximo]
            while _utf16(t) > maximo:
                t = t[:-1]
            return t
    return None


def cliente_do_pedido(order: Order, entry: dict, check: dict | None, av: Avaliacao) -> dict:
    """`cliente` do corpo: CNPJ que resolveu a rede; nome do override, do
    cabeçalho ou, sem eles, a razão do Fire."""
    nome = _texto(
        entry.get("cliente_override_razao")
        if entry.get("cliente_override_codigo") is not None
        else None,
        order.header.customer_name,
        ((check or {}).get("client") or {}).get("razao_social"),
        maximo=255,
    )
    return {"nome": nome, "cnpj": av.cnpj_rede}


def montar_corpo(
    import_id: str,
    order: Order,
    av: Avaliacao,
    *,
    empresa_cnpj: str,
    cliente: dict,
    emitido_em: str | None,
    recebido_em: str,
    cursor: str | None,
    check: dict | None = None,
) -> tuple[dict, str]:
    """Corpo `aprovacaoEnvioV1` (`tipo: "pedido"`) sem `revisao`, e o
    `conteudoHash`. Vão todos os itens, `ok` e `na_fila`. Sem validar: quem
    chama passa por `precos_schema.validar_envio` antes de gravar.
    `check`: o do envio (com os vínculos), para a descrição do Fire."""
    h = conteudo_hash(order, av, cliente_cnpj=cliente.get("cnpj"), empresa_cnpj=empresa_cnpj)
    check_itens = (check or {}).get("items") or []
    itens = []
    for it in av.itens:
        i = it.item_index
        oi = order.items[i]
        fire = check_itens[i] if i < len(check_itens) else {}
        q = _quantidade_2(oi.quantity)
        ok = it.situacao == "ok"
        itens.append(
            {
                "itemChave": item_chave(import_id, i),
                "linha": i,
                "erpProdutoId": it.fire_produto_id,
                "codigo": _texto(oi.product_code, it.fire_produto_id, maximo=128),
                "descricao": _texto(
                    oi.description,
                    fire.get("fire_description"),
                    oi.product_code,
                    it.fire_produto_id,
                    maximo=512,
                ),
                "unidadeErp": it.unidade_erp,
                "quantidade": float(q) if q is not None and q > 0 else oi.quantity,
                "precoPedidoCentavos": it.preco_pedido_centavos,
                "precoTabelaCentavos": it.preco_tabela_centavos,
                "precoTabelaId": it.preco_tabela_id,
                "situacao": "ok" if ok else "na_fila",
                "motivo": None if ok else it.motivo,
            }
        )
    corpo = {
        "schema": "precos.aprovacao.v1",
        "tipo": "pedido",
        "externalId": import_id,
        "conteudoHash": h,
        "empresaFaturadoraCnpj": empresa_cnpj,
        "pedidoNumero": order.header.order_number,
        "emitidoEm": emitido_em,
        "recebidoEm": recebido_em,
        "cliente": cliente,
        "redeId": av.rede.id if av.rede is not None else None,
        "tabelaCursor": cursor,
        "itens": itens,
    }
    return corpo, h


def _acao_revisao(vig: dict | None, h: str, tem_fila: bool) -> str:
    """O que a revisão vigente vira diante do conteúdo atual:
    `vigente` (decisões dela valem) | `nova` (com fila: revisão nova) |
    `retirar` (sem fila: libera pela regra local e tira do Flow) | `local`."""
    obsoleta = vig is not None and (vig["conteudo_hash"] != h or vig["situacao"] == "recusada_flow")
    if tem_fila:
        return "nova" if vig is None or obsoleta else "vigente"
    if vig is None:
        return "local"
    nunca_tentada = (
        vig["situacao"] == "pendente_envio"
        and vig.get("enviada_em") is None
        and vig.get("tentada_em") is None
    )
    # Sem fila, uma revisão que o Flow nunca viu não espera nada: sai local.
    return "retirar" if obsoleta or nunca_tentada else "vigente"


def _bloqueio_do_corpo(order: Order) -> str | None:
    """Texto legível para o que o schema do envio recusaria no pedido."""
    numero = order.header.order_number
    if not isinstance(numero, str) or not numero.strip():
        return "número do pedido ausente"
    if _utf16(numero) > 64:
        return "número do pedido com mais de 64 caracteres"
    for i, it in enumerate(order.items):
        q = it.quantity
        if q is None or not math.isfinite(q) or q <= 0:
            return f"item {i + 1} sem quantidade maior que zero"
        if _quantidade_2(q) != Decimal(str(q)):
            return f"item {i + 1} com quantidade de mais de 2 casas decimais ({q})"
    return None


def _vai_ao_flow(av: Avaliacao) -> bool:
    """Pedido que pode ter revisão: nenhum item não avaliado nem intercompany
    (contrato §4.1: esses nunca chegam ao Flow)."""
    return not any(it.situacao in ("nao_avaliado", "fora_intercompany") for it in av.itens)


def classificar_itens(
    order: Order,
    av: Avaliacao,
    revisao: dict | None,
    decisoes: dict[str, dict[str, Any]],
) -> tuple[dict[int, str], dict[int, float], dict[int, Any]]:
    """Estado final de cada item — a ÚNICA fonte do portão e do preview (§1.6).

    Estados: ok | aguardando | aprovado | recusado | nao_avaliado | intercompany.
    Sem revisão vigente: item `ok` local fica `ok`, fila fica `aguardando`.
    Com revisão: cada item usa a decisão da sua `itemChave` (`recusado` →
    recusado; outra → aprovado com `precoAprovadoCentavos/100`; sem decisão
    → aguardando); revisão `recusada_flow` → todos recusados; revisão com
    `bloqueio_local` → todos não avaliados.
    Devolve também o preço a aplicar e o `decisaoId` de cada item aprovado."""
    estados: dict[int, str] = {}
    precos: dict[int, float] = {}
    decisao_ids: dict[int, Any] = {}
    for it in av.itens:
        i = it.item_index
        if it.situacao == "fora_intercompany":
            estados[i] = "intercompany"
        elif it.situacao == "nao_avaliado":
            estados[i] = "nao_avaliado"
        elif revisao is None:
            estados[i] = "ok" if it.situacao == "ok" else "aguardando"
        elif revisao.get("bloqueio_local"):
            estados[i] = "nao_avaliado"
        elif revisao.get("situacao") == "recusada_flow":
            estados[i] = "recusado"
        else:
            d = decisoes.get(item_chave(revisao["import_id"], i))
            if d is None or (
                d.get("resultado") != "recusado" and d.get("preco_aprovado_centavos") is None
            ):
                estados[i] = "aguardando"
            elif d.get("resultado") == "recusado":
                estados[i] = "recusado"
            else:
                estados[i] = "aprovado"
                precos[i] = int(d["preco_aprovado_centavos"]) / 100
                decisao_ids[i] = d.get("decisao_id")
    return estados, precos, decisao_ids


def _reais(centavos: int | None) -> float | None:
    return None if centavos is None else round(centavos / 100, 2)


def _copia_do_preview(agora: datetime) -> dict:
    """Frescor da cópia local de preços para o preview (`ultima_sync_ok`)."""
    with db.connect() as conn:
        sync = precos_repo.ultima_sync_ok(conn)
    if sync is None:
        return {"sincronizada_em": None, "idade_min": None}
    idade = max(0, int((agora - sync).total_seconds() // 60))
    return {"sincronizada_em": sync.isoformat(), "idade_min": idade}


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
    (mesmo `classificar_itens` e a mesma leitura da revisão). Só leitura: sem
    Fire, sem audit, nunca cria nem retira revisão. `desligado` → None."""
    check = _check_utilizavel(entry.get("check"), order)
    av = avaliar_entry(entry, order, env=env, agora=agora, produtos_por_item=produtos_por_item)
    if av is None:
        return None
    bloqueio = bloqueio_do_pedido(order, entry, check)
    revisao, decisoes = None, {}
    if _modo(env) == "ligado" and bloqueio is None and _vai_ao_flow(av):
        with db.connect() as conn:
            vig = revs.vigente(conn, import_id)
            empresa = preco_regra.normalizar_cnpj((env or {}).get("cnpj")) or ""
            h = conteudo_hash(order, av, cliente_cnpj=av.cnpj_rede, empresa_cnpj=empresa)
            acao = _acao_revisao(vig, h, bool(av.pendentes))
            if acao == "vigente" and vig.get("bloqueio_local"):
                revisao = vig
                bloqueio = (
                    f"Pedido não pode ir ao Flow para aprovação de preço: {vig['bloqueio_local']}."
                )
            elif acao == "vigente":
                revisao, decisoes = vig, revs.decisoes_da_vigente(conn, import_id)
            elif (
                acao == "nova"
                and vig is not None
                and vig["situacao"] == "recusada_flow"
                and vig["conteudo_hash"] == h
            ):
                revisao = vig  # mostra o erro do Flow; o próximo envio cria a seguinte
            elif acao == "nova":
                # o portão criaria a revisão seguinte, ainda sem decisão
                revisao = {"import_id": import_id, "situacao": "pendente_envio"}
    estados, _, _ = classificar_itens(order, av, revisao, decisoes)
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
        d = decisoes.get(item_chave(import_id, it.item_index)) or {}
        itens.append(
            {
                "item_index": it.item_index,
                "situacao": it.situacao,
                "motivo": it.motivo,
                "preco_pedido": _reais(it.preco_pedido_centavos),
                "preco_tabela": _reais(it.preco_tabela_centavos),
                "decisao": d.get("resultado"),
                "estado": estado,
            }
        )
    return {
        "modo": _modo(env),
        "itens": itens,
        "resumo": resumo,
        "bloqueio_pedido": bloqueio,
        "copia": _copia_do_preview(agora),
        "revisao": None
        if revisao is None or "revisao" not in revisao
        else {
            "numero": revisao["revisao"],
            "situacao": revisao["situacao"],
            "erro_flow": revisao.get("erro_definitivo"),
        },
    }


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
    reason: str, detalhe: str, av: Avaliacao, decisoes_por_item: dict[int, dict[str, Any]]
) -> DecisaoEnvio:
    itens = [
        {
            "item_index": it.item_index,
            "fire_produto_id": it.fire_produto_id,
            "situacao": it.situacao,
            "motivo": it.motivo,
            "decisao": (decisoes_por_item.get(it.item_index) or {}).get("resultado"),
        }
        for it in av.itens
        if it.situacao != "ok" or it.item_index in decisoes_por_item
    ]
    return DecisaoEnvio(
        True,
        reason=reason,
        detalhe=detalhe,
        block_detail={"preco_trava": "ligado", "reason": reason, "itens": itens},
        metric_labels=[reason],
        avaliacao=av,
    )


def _revisao_do_envio(
    import_id: str,
    order: Order,
    entry: dict,
    check: dict,
    av: Avaliacao,
    agora: datetime,
    env: dict | None,
) -> tuple[dict | None, dict[str, dict[str, Any]], str | None, str | None]:
    """Revisão que vale para este envio, com as decisões dela. Só chamado com
    o pedido avaliável (nenhum bloqueio de pedido nem item não avaliado).
    Com fila: corpo validado pelo schema e revisão garantida (nova se o hash
    mudou ou a vigente foi recusada pelo Flow). Sem fila e vigente obsoleta:
    ela é retirada e o pedido segue pela regra local.
    Devolve (revisão, decisões, corpo inválido, erro do Flow da revisão anterior)."""
    empresa = preco_regra.normalizar_cnpj((env or {}).get("cnpj"))
    if empresa is None:  # o avaliador já devolve não avaliado; defesa (fail-closed)
        raise ValueError("ambiente sem CNPJ da empresa faturadora")
    with db.connect() as conn:
        vig = revs.vigente(conn, import_id)
        corpo, h = montar_corpo(
            import_id,
            order,
            av,
            empresa_cnpj=empresa,
            cliente=cliente_do_pedido(order, entry, check, av),
            emitido_em=emitido_em_do_pedido(order),
            recebido_em=recebido_em_do_pedido(entry.get("imported_at"), agora),
            cursor=precos_repo.estado_sync(conn).cursor,
            check=check,
        )
        acao = _acao_revisao(vig, h, bool(av.pendentes))
        if acao == "local":
            return None, {}, None, None
        if acao == "retirar":
            revs.retirar(conn, import_id, MOTIVO_RETIRADA_LOCAL, em=agora)
            return None, {}, None, None
        erro_anterior = None
        if acao == "nova":
            legivel = _bloqueio_do_corpo(order)
            if legivel is not None:
                return None, {}, legivel, None
            try:
                precos_schema.validar_envio({**corpo, "revisao": 1})
            except ValueError as exc:
                return None, {}, _erro_de_schema(exc), None
            if vig is not None and vig["situacao"] == "recusada_flow":
                erro_anterior = vig.get("erro_definitivo")
            revs.garantir_revisao(conn, import_id, corpo, h, agora=agora)
            vig = revs.vigente(conn, import_id)
        if vig is not None and vig.get("bloqueio_local"):
            # Corpo que não pode ir ao Flow: bloqueia até o conteúdo mudar.
            return None, {}, vig["bloqueio_local"], None
        return vig, revs.decisoes_da_vigente(conn, import_id), None, erro_anterior


def _erro_de_schema(exc: ValueError) -> str:
    erros = getattr(exc, "errors", None)
    if callable(erros):
        partes = []
        for e in erros()[:3]:
            local = ".".join(str(p) for p in e.get("loc", ()))
            partes.append(f"{local}: {e.get('msg')}" if local else str(e.get("msg")))
        return "; ".join(partes)
    return str(exc)


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
    # Check da regra de hoje (intercompany) e do bloqueio de pedido: sempre o
    # do próprio ambiente. O da avaliação vem de `_check_para_preco`.
    check_hoje = _com_vinculos(
        _check_utilizavel(live, order) or _check_utilizavel(entry.get("check"), order),
        produtos_por_item,
    )
    vazio = Avaliacao(rede=None, cnpj_rede=None)
    revisao: dict | None = None
    decisoes: dict[str, dict[str, Any]] = {}
    erro_anterior: str | None = None
    try:
        # Bloqueio de nível de pedido nunca toca revisão nem decisão: erro de
        # configuração ou Fire fora não pode invalidar decisão já dada.
        bloqueio = bloqueio_do_pedido(order, entry, check_hoje)
        if bloqueio is not None:
            return _bloqueio_trava("preco_nao_avaliado", bloqueio, vazio, {})
        check = _check_para_preco(order, entry, live, env, produtos_por_item)
        if check is None:
            av = _todos(order, None, "nao_avaliado", "produto_nao_identificado")
            return _bloqueio_trava(
                "preco_nao_avaliado",
                f"{len(order.items)} item(ns) sem avaliação de preço: "
                f"{_MOTIVO_LEGIVEL['produto_nao_identificado']}.",
                av,
                {},
            )
        av = _avaliar_local(order, check, entry, agora, env)
        if any(it.situacao == "fora_intercompany" for it in av.itens):
            # Fora da trava agora (override para empresa do grupo): sai da fila do Flow.
            with db.connect() as conn:
                if revs.vigente(conn, import_id) is not None:
                    revs.retirar(conn, import_id, MOTIVO_RETIRADA_INTERCOMPANY, em=agora)
        if _vai_ao_flow(av):
            revisao, decisoes, corpo_invalido, erro_anterior = _revisao_do_envio(
                import_id, order, entry, check, av, agora, env
            )
            if corpo_invalido is not None:
                return _bloqueio_trava(
                    "preco_nao_avaliado",
                    f"Pedido não pode ir ao Flow para aprovação de preço: {corpo_invalido}.",
                    av,
                    {},
                )
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
        hoje = _regra_de_hoje(check_hoje, ack_items)
        hoje.avaliacao = av
        return hoje

    estados, precos, decisao_ids = classificar_itens(order, av, revisao, decisoes)
    por_item = {
        it.item_index: decisoes[item_chave(import_id, it.item_index)]
        for it in av.itens
        if item_chave(import_id, it.item_index) in decisoes
    }
    bloqueados = [it for it in av.itens if estados[it.item_index] == "nao_avaliado"]
    recusados = [i for i, e in estados.items() if e == "recusado"]
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
        if revisao is not None and revisao.get("situacao") == "recusada_flow":
            motivos = revisao.get("erro_definitivo") or "sem motivo informado"
        else:
            motivos = "; ".join(
                dict.fromkeys(
                    (por_item[i].get("motivo_recusa") or "sem motivo informado") for i in recusados
                )
            )
        partes.append(f"Preço recusado no Flow: {motivos}.")
        reason = reason or "preco_recusado"
    if aguardando:
        partes.append(f"Pedido aguardando aprovação de preço no Flow: {len(aguardando)} item(ns).")
        if erro_anterior:
            partes.append(f"A revisão anterior foi recusada pelo Flow: {erro_anterior}.")
        reason = reason or "preco_aguardando"
    if reason is not None:
        return _bloqueio_trava(reason, " ".join(partes), av, por_item)

    aplicados = [
        {"item_index": i, "preco": p, "flow_id": decisao_ids[i]} for i, p in precos.items()
    ]
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
            check = _check_para_preco(order, entry, live, env, produtos_por_item)
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
