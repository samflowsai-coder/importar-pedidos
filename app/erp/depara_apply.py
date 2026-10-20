"""Aplica o de-para no Order antes de gerar o XLS ou inserir no Fire.

Item resolvido por vínculo sai com a identidade do Fire (codigo/ean), com a
referência original do varejista anexada ao OBS (rastreabilidade). Mutação
transiente: o Order vem de Order.model_validate(snapshot), descartado após o
export. Não toca o snapshot persistido.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.persistence import produto_depara_repo
from app.persistence.produto_depara_repo import _norm_key

if TYPE_CHECKING:
    import sqlite3

    from app.models.order import Order


def resolver(order: Order, *, conn: sqlite3.Connection) -> list[dict]:
    """Vínculos de-para dos itens do `order`, SEM mutar nada.

    Uma entrada por item vinculado: `item_index`, `fire_produto_id`,
    `fire_codigo`, `fire_ean`, `orig`. É o mapa que o envio consulta UMA vez
    antes do portão de preço: a trava avalia o mesmo SEQ que o
    `FirebirdExporter` grava e cujo código o XLSX leva (`aplicar`).

    Chave do cliente: `produto_depara_repo.client_key()` — CNPJ (dígitos)
    quando o header tem, senão o nome normalizado (varejistas como
    Riachuelo não trazem CNPJ no header; o CNPJ real é por loja).

    Prioridade de match por item: código primeiro, EAN como fallback —
    mesma ordem do 3º degrau de `product_check.check_order` (mantém os dois
    consumidores do de-para consistentes).
    """
    key = produto_depara_repo.client_key(order.header.customer_cnpj, order.header.customer_name)
    codigos = [it.product_code for it in order.items if it.product_code]
    eans = [it.ean for it in order.items if it.ean]
    dm = produto_depara_repo.lookup(conn, key, codigos=codigos, eans=eans)
    if not dm:
        return []

    vinculos: list[dict] = []
    for idx, it in enumerate(order.items):
        dk = None
        if it.product_code:
            dk = ("codigo", _norm_key("codigo", it.product_code))
        if (dk is None or dk not in dm) and it.ean:
            dk = ("ean", _norm_key("ean", it.ean))
        dv = dm.get(dk) if dk else None
        if dv is None:
            continue
        vinculos.append(
            {
                "item_index": idx,
                "fire_produto_id": dv["fire_produto_id"],
                "fire_codigo": dv["fire_codigo"],
                "fire_ean": dv.get("fire_ean"),
                "orig": it.product_code or it.ean or "",
            }
        )
    return vinculos


def produtos_por_item(vinculos: list[dict]) -> dict[int, int]:
    """{posição em order.items: SEQ} para o `FirebirdExporter` e para o portão."""
    return {v["item_index"]: int(v["fire_produto_id"]) for v in vinculos}


def sobrepor_escolhas(order: Order, vinculos: list[dict], escolhidos: dict | None) -> list[dict]:
    """Vínculos com a escolha pontual de cada linha por cima (R18/R20).

    `escolhidos`: `entry["produto_escolhido"]` do import. A linha escolhida sai
    com o SEQ escolhido (insert) e o `CODPROD_ALTERN` gravado na escolha, o EAN
    dele (ou nenhum: o EAN do cliente é da linha, não do produto escolhido) e o
    código do cliente na OBS. Vale em todo modo; as outras linhas ficam como vieram.

    Produto sem `CODPROD_ALTERN` (R21): nunca vai o SEQ em texto (o importador de
    Excel do Fire casaria por PREFIXO); a linha fica com o código do cliente e
    `sem_codigo_fire: True` — só o insert (pelo SEQ) aceita; a planilha recusa
    (`pontuais_sem_codigo`). `codigo_vivo_das_escolhas` troca pelo código lido
    do Fire agora, quando o check traz o produto."""
    from app.erp.product_check import escolhas_do_import  # evita ciclo

    seqs = escolhas_do_import({"produto_escolhido": escolhidos})
    if not seqs:
        return vinculos
    detalhe = {int(k): v for k, v in (escolhidos or {}).items() if str(k).isdigit()}
    out = [v for v in vinculos if v["item_index"] not in seqs]
    for i, seq in seqs.items():
        if not 0 <= i < len(order.items):
            continue
        e = detalhe.get(i) or {}
        it = order.items[i]
        codigo = (str(e.get("codigo") or "")).strip()
        out.append(
            {
                "item_index": i,
                "fire_produto_id": seq,
                "fire_codigo": codigo or it.product_code,
                "codigo_cliente": it.product_code,
                "sem_codigo_fire": not codigo,
                "fire_ean": (str(e.get("ean") or "")).strip(),
                "substitui_ean": True,
                "orig": it.product_code or it.ean or "",
                "fonte": "escolha_pontual",
            }
        )
    return sorted(out, key=lambda v: v["item_index"])


def codigo_vivo_das_escolhas(vinculos: list[dict], check: dict | None) -> list[dict]:
    """Escolhas pontuais com o `CODPROD_ALTERN` lido do Fire no check ao vivo
    (o candidato decidido do item `ok` por `escolha_pontual` no mesmo SEQ), que
    vale mais que o do catálogo guardado na escolha. Sem esse candidato no
    check (Fire fora, outro SEQ), fica o da escolha. Candidato sem código no
    Fire = sem código, mesmo que o catálogo guardado tivesse um."""
    from app.erp.produto_trava import candidato_decidido  # evita ciclo

    itens = (check or {}).get("items") or []

    def _candidato(v: dict) -> dict | None:
        i = v["item_index"]
        if v.get("fonte") != "escolha_pontual" or not 0 <= i < len(itens):
            return None
        produto = (itens[i] or {}).get("produto")
        if (
            not isinstance(produto, dict)
            or produto.get("estado") != "ok"
            or produto.get("fonte") != "escolha_pontual"
            or str(produto.get("fire_seq")) != str(v["fire_produto_id"])
        ):
            return None
        return candidato_decidido(produto)

    out: list[dict] = []
    for v in vinculos:
        cand = _candidato(v)
        if cand is None:
            out.append(v)
            continue
        codigo = (cand.get("codprod_altern") or "").strip()
        out.append(
            {
                **v,
                "fire_codigo": codigo or v.get("codigo_cliente"),
                "sem_codigo_fire": not codigo,
            }
        )
    return out


def pontuais_sem_codigo(vinculos: list[dict]) -> list[dict]:
    """Linhas com escolha pontual cujo produto não tem `CODPROD_ALTERN`: a
    planilha não consegue apontá-las (R21)."""
    return [v for v in vinculos if v.get("fonte") == "escolha_pontual" and v.get("sem_codigo_fire")]


def pontuais_invalidas(vinculos: list[dict], check: dict | None) -> list[dict]:
    """Linhas com escolha pontual que o check ao vivo marca `vinculo_invalido`: o
    SEQ escolhido sumiu ou foi bloqueado no Fire (M1). Não sai em nenhum modo.
    Fire fora (check sem a decisão, `fire_indisponivel`) não afirma nada."""
    itens = (check or {}).get("items") or []
    out: list[dict] = []
    for v in vinculos:
        i = v["item_index"]
        if v.get("fonte") != "escolha_pontual" or not 0 <= i < len(itens):
            continue
        produto = (itens[i] or {}).get("produto")
        if isinstance(produto, dict) and produto.get("estado") == "vinculo_invalido":
            out.append(v)
    return out


def motivo_pontual_invalida(v: dict) -> str:
    return (
        f"O produto {v['fire_produto_id']} escolhido para a linha {v['item_index'] + 1} "
        "não está mais ativo no Fire; escolha outro."
    )


def motivo_pontual_sem_codigo(v: dict) -> str:
    return (
        f"O produto {v['fire_produto_id']} escolhido para a linha {v['item_index'] + 1} "
        "não tem código no Fire; a planilha não consegue apontá-lo. "
        "Use o envio direto ou ajuste o cadastro."
    )


def aplicar(order: Order, vinculos: list[dict]) -> list[dict]:
    """Enriquece os itens do `order` com a identidade Fire dos `vinculos`
    (de `resolver`). Muta `order.items` in place e retorna o resumo do que mudou."""
    changed: list[dict] = []
    for v in vinculos:
        it = order.items[v["item_index"]]
        it.product_code = v["fire_codigo"]
        if v.get("substitui_ean"):
            # Produto decidido (`produto_trava.produtos_decididos`): o EAN dele,
            # ou nenhum — nunca fica o EAN do cliente.
            it.ean = v.get("fire_ean") or None
        elif v.get("fire_ean"):
            it.ean = v["fire_ean"]
        ref_note = f"ref cliente: {v['orig']}"
        it.obs = f"{it.obs} | {ref_note}" if it.obs else ref_note
        changed.append(
            {
                "item_index": v["item_index"],
                "fire_produto_id": v["fire_produto_id"],
                "fire_codigo": v["fire_codigo"],
                "orig": v["orig"],
            }
        )
    return changed


def apply(order: Order, *, conn: sqlite3.Connection) -> list[dict]:
    """`resolver` + `aplicar` numa chamada (mesmo contrato de antes)."""
    return aplicar(order, resolver(order, conn=conn))
