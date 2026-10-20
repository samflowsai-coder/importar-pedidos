"""Match order items against Fire's product catalog before importing.

Read-only. Uses the same lookups as FirebirdExporter (EAN first, then
CODPROD_ALTERN) plus the client CNPJ resolution. Returns a structured report
the UI can render with green / amber / red indicators.

Graceful when FB_DATABASE is not set: returns a report flagged as unavailable
so the preview still loads.
"""

from __future__ import annotations

import re
from typing import Any

from app.erp import produto_escopo, queries
from app.erp.connection import FirebirdConnection
from app.erp.produto_chave import (
    MOTIVO_CHAVES_DISCORDAM,
    Candidato,
    DecisaoProduto,
    decidir,
    motivo_vinculo_invalido,
)
from app.models.order import Order
from app.persistence import db, produto_depara_repo, produto_vinculo_repo
from app.persistence.context import NoActiveEnvironmentError
from app.persistence.produto_depara_repo import _norm_key as _depara_norm_key

# Estado do item["produto"] quando o Fire não pôde ser consultado. Não é um
# resultado de `decidir`: é do check, e nunca vale como produto conferido.
FIRE_INDISPONIVEL = "fire_indisponivel"
MOTIVO_FIRE_INDISPONIVEL = "O Fire não respondeu; o produto não pôde ser conferido."


def _cnpj_digits(cnpj: str | None) -> str:
    if not cnpj:
        return ""
    return re.sub(r"\D", "", cnpj)


def _chunked(values: list, size: int = 200):
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _fetch_map_by_key(cur, sql_builder, values: list) -> dict:
    """Roda o SELECT batelado (chunk de 200) e devolve {key: (seq, desc, preco, unidade)}.

    O SELECT tem a chave como 1ª coluna e UNIDADE como a 5ª. Valores
    deduplicados pelo chamador.
    """
    out: dict = {}
    for chunk in _chunked(values):
        cur.execute(sql_builder(len(chunk)), tuple(chunk))
        for row in cur.fetchall():
            key = row[0]
            # first-wins + ORDER BY SEQ replica o ROWS 1 determinístico do lookup antigo
            out.setdefault(key, (row[1], row[2], row[3], row[4]))
    return out


# CODPROD_ALTERN é VARCHAR(30) na MM e na Nasmar (conferido 24/09/2026). Parâmetro
# maior que a coluna estoura "string right truncation" no Firebird, e um código
# maior que 30 não é prefixo de nada.
_CODPROD_ALTERN_MAX = 30
_LOTE_PREVISAO = 150


def _prever_import_fire(cur, codigos: list[str]) -> dict[str, tuple]:
    """{codigo: (seq, codprod_altern, descricao)} do produto que o importador de
    Excel do Fire gravaria para cada código.

    O Fire casa por PREFIXO e fica com o primeiro da ordem física (BACKLOG 2.15).
    A query volta nessa ordem, então o primeiro registro que começa com o código
    é o palpite dele. Código sem nenhum candidato fica fora do dict.
    """
    out: dict[str, tuple] = {}
    validos = [c for c in codigos if c.strip() and len(c) <= _CODPROD_ALTERN_MAX]
    for lote in _chunked(validos, _LOTE_PREVISAO):
        cur.execute(queries.prever_import_fire_sql(len(lote)), tuple(lote))
        pendentes = set(lote)
        for codprod, seq, desc in cur.fetchall():
            if not pendentes:
                break
            if codprod is None:
                continue
            for c in [c for c in pendentes if codprod.startswith(c)]:
                out[c] = (seq, codprod.strip(), desc)
                pendentes.discard(c)
    return out


def _to_cents(value: float | None) -> int | None:
    """Converte reais em centavos (int) para comparação sem drift de float."""
    if value is None:
        return None
    return int(round(float(value) * 100))


def _classify_price(
    unit_price_order: float | None,
    fire_preco_venda: float | None,
) -> str:
    """Determina price_status para um item COM match de produto.

    Não chame para itens sem match — use 'no_product_match' diretamente.
    """
    if unit_price_order is None:
        return "no_order_price"
    if fire_preco_venda is None or _to_cents(fire_preco_venda) == 0:
        return "no_price_in_fire"
    if _to_cents(unit_price_order) == _to_cents(fire_preco_venda):
        return "match"
    return "mismatch"


def _empty_item_result(
    product_code: str | None,
    ean: str | None,
    unit_price_order: float | None,
) -> dict:
    return {
        "product_code": product_code,
        "ean": ean,
        "match": False,
        "match_source": None,
        "fire_product_id": None,
        "fire_description": None,
        "fire_preco_venda": None,
        "fire_unidade": None,
        "unit_price_order": unit_price_order,
        "price_status": "no_product_match",
        "price_diff": None,
        "troca_no_fire": None,
    }


def _produto_indisponivel() -> dict:
    return {
        "estado": FIRE_INDISPONIVEL,
        "fire_seq": None,
        "fonte": None,
        "motivo": MOTIVO_FIRE_INDISPONIVEL,
        "candidatos": [],
    }


def _produto_dict(d: DecisaoProduto) -> dict:
    return {
        "estado": d.estado,
        "fire_seq": d.fire_seq,
        "fonte": d.fonte,
        "motivo": d.motivo,
        "candidatos": [
            {
                "seq": c.seq,
                "descricao": c.descricao,
                "codprod_altern": c.codprod_altern,
                "bloqueado": c.bloqueado,
            }
            for c in d.candidatos
        ],
    }


def _txt(value) -> str | None:
    return (str(value).strip() or None) if value is not None else None


def _candidato(row) -> Candidato:
    """Linha de `find_produtos_chave_por_*_sql` (chave primeiro, 7 colunas)."""
    _, seq, desc, preco, unidade, bloqueado, codprod = row
    return Candidato(
        seq=int(seq),
        descricao=_txt(desc) or "",
        codprod_altern=_txt(codprod),
        # Fire grava 'Sim'/'Nao' ou NULL: ativo = qualquer coisa que não seja 'Sim'.
        bloqueado=(_txt(bloqueado) or "").upper() == "SIM",
        unidade=_txt(unidade),
        preco_venda=float(preco) if preco is not None else None,
    )


def _candidatos_por_chave(cur, sql_builder, valores: list, chave_de) -> dict:
    """{chave: [Candidato, ...]} com TODAS as linhas de cada chave, em ORDER BY SEQ."""
    out: dict = {}
    for chunk in _chunked(valores):
        cur.execute(sql_builder(len(chunk)), tuple(chunk))
        for row in cur.fetchall():
            out.setdefault(chave_de(row[0]), []).append(_candidato(row))
    return out


def _cnpj_real(order: Order, env: dict | None) -> tuple[str | None, bool]:
    """(cliente real do pedido intercompany, conferiu?) — ruling R1.

    Falha vira (None, False): o escopo cai no cliente do pedido, e o check
    marca a conferência como incompleta (o vínculo do cliente real pode existir).
    """
    slug = (env or {}).get("slug")
    if not slug:
        return None, True
    try:
        from app.integrations.flowpcp.intercompany import resolucao_para  # evita ciclo

        r = resolucao_para(order, slug=slug)
    except Exception as exc:  # noqa: BLE001 — o check nunca cai por causa do intercompany
        from app.utils.logger import logger

        logger.warning(f"check: cliente real do intercompany falhou ({type(exc).__name__}): {exc}")
        return None, False
    if r is None:
        return None, True
    if r.resolvido:
        return r.cnpj, True
    # resolucao_para engole a própria falha e devolve erro_conexao.
    return None, r.motivo != "erro_conexao"


def _vinculos_v2(
    order: Order,
    env: dict | None,
    codigos: list[str | None],
    eans: list[str | None],
) -> tuple[list[tuple[int | None, int | None]], bool]:
    """((SEQ do vínculo do código, SEQ do vínculo do EAN) por item, completa?).

    Lê o SQLite do ambiente ativo, no escopo de cada item. Sem ambiente ativo:
    não há vínculo a ler, a conferência é completa. Qualquer outra falha (banco
    fora, intercompany sem resposta): segue sem vínculo e devolve completa=False,
    para a trava (Task 4) fechar em vez de confiar no resultado.
    """
    vazio: list[tuple[int | None, int | None]] = [(None, None)] * len(order.items)
    if not any(codigos) and not any(eans):
        return vazio, True
    try:
        cnpj_real, completa = _cnpj_real(order, env)
        out = list(vazio)
        with db.connect() as sconn:
            escopos_por_cnpj: dict[str | None, tuple[str, ...]] = {}
            grupos: dict[tuple[str, ...], list[int]] = {}
            for i in range(len(order.items)):
                if not (codigos[i] or eans[i]):
                    continue
                cnpj = produto_escopo.cnpj_do_item(order, i, cnpj_real=cnpj_real)
                if cnpj not in escopos_por_cnpj:
                    escopos_por_cnpj[cnpj] = tuple(produto_escopo.escopos_para(sconn, cnpj))
                escopos = escopos_por_cnpj[cnpj]
                if escopos:
                    grupos.setdefault(escopos, []).append(i)
            for escopos, indices in grupos.items():
                achados = produto_vinculo_repo.lookup(
                    sconn,
                    list(escopos),
                    codigos=list(dict.fromkeys(c for i in indices if (c := codigos[i]))),
                    eans=list(dict.fromkeys(e for i in indices if (e := eans[i]))),
                )
                for i in indices:
                    vc = achados.get(("codigo", codigos[i])) if codigos[i] else None
                    ve = achados.get(("ean", eans[i])) if eans[i] else None
                    out[i] = (
                        int(vc.fire_seq) if vc is not None else None,
                        int(ve.fire_seq) if ve is not None else None,
                    )
        return out, completa
    except NoActiveEnvironmentError:
        return vazio, True
    except Exception as exc:  # noqa: BLE001 — banco do ambiente fora: sem vínculo, incompleta
        from app.utils.logger import logger

        logger.warning(f"vínculo v2 não pôde ser lido ({type(exc).__name__}): {exc}")
        return vazio, False


def _vinculos_discordam(v_cod: int, v_ean: int, por_seq: dict) -> DecisaoProduto:
    """Vínculo do código e vínculo do EAN apontam para SEQs diferentes.

    R7(a): vínculo inválido é final, então um dos dois sumido/bloqueado dá
    vinculo_invalido. Os dois válidos: chaves_discordam, nenhum ganha em silêncio.
    """
    for seq in (v_cod, v_ean):
        c = (por_seq.get(seq) or [None])[0]
        if c is None or c.bloqueado:
            return DecisaoProduto(
                "vinculo_invalido",
                None,
                None,
                (c,) if c is not None else (),
                motivo_vinculo_invalido(seq),
            )
    return DecisaoProduto(
        "chaves_discordam",
        None,
        None,
        (por_seq[v_ean][0], por_seq[v_cod][0]),
        MOTIVO_CHAVES_DISCORDAM,
    )


def _decidir_itens(
    cur,
    order: Order,
    codigos: list[str | None],
    eans: list[str | None],
    vinculos: list[tuple[int | None, int | None]],
) -> list[dict]:
    """item["produto"] de cada item: candidatos por chave exata, numa passada batelada."""
    eans_q = list(dict.fromkeys(e for e in eans if e))
    # Código maior que a coluna nunca é igual a nada (e estoura truncation no Fire).
    cods_q = list(dict.fromkeys(c for c in codigos if c and len(c) <= _CODPROD_ALTERN_MAX))
    seqs_q = list(dict.fromkeys(s for par in vinculos for s in par if s is not None))

    por_ean = (
        _candidatos_por_chave(
            cur, queries.find_produtos_chave_por_ean_sql, eans_q, lambda k: (k or "").strip()
        )
        if eans_q
        else {}
    )
    por_codigo = (
        _candidatos_por_chave(
            cur,
            queries.find_produtos_chave_por_codigo_sql,
            cods_q,
            lambda k: (k or "").strip().upper(),
        )
        if cods_q
        else {}
    )
    por_seq = (
        _candidatos_por_chave(cur, queries.find_produtos_chave_por_seq_sql, seqs_q, int)
        if seqs_q
        else {}
    )

    out: list[dict] = []
    for codigo, ean, (v_cod, v_ean) in zip(codigos, eans, vinculos, strict=True):
        if v_cod is not None and v_ean is not None and v_cod != v_ean:
            out.append(_produto_dict(_vinculos_discordam(v_cod, v_ean, por_seq)))
            continue
        vinculo_seq = v_cod if v_cod is not None else v_ean
        vinculo_candidato = (
            (por_seq.get(vinculo_seq) or [None])[0] if vinculo_seq is not None else None
        )
        decisao = decidir(
            codigo=codigo,
            ean=ean,
            por_ean=por_ean.get(ean, []) if ean else [],
            por_codigo=por_codigo.get(codigo, []) if codigo else [],
            vinculo_seq=vinculo_seq,
            vinculo_candidato=vinculo_candidato,
        )
        out.append(_produto_dict(decisao))
    return out


def check_order(order: Order, *, env: dict | None = None) -> dict:
    """Return match report for the order. Safe to call without Fire configured."""
    conn_mgr = FirebirdConnection()

    def _item_indisponivel(it) -> dict:
        entry = _empty_item_result(it.product_code, it.ean, it.unit_price)
        entry["produto"] = _produto_indisponivel()
        return entry

    unavailable: dict[str, Any] = {
        "available": False,
        "reason": "FB_DATABASE_NOT_SET",
        "produto_conferencia_incompleta": False,
        "client": {
            "match": False,
            "fire_id": None,
            "razao_social": None,
            "cnpj": order.header.customer_cnpj,
        },
        "items": [_item_indisponivel(it) for it in order.items],
        "summary": {
            "items_total": len(order.items),
            "items_matched": 0,
            "items_missing": len(order.items),
            "client_matched": False,
            "items_troca_no_fire": 0,
            "troca_no_fire_checked": False,
            "items_produto_pendente": len(order.items),
            "produto_conferencia_incompleta": False,
            "price_summary": {
                "items_match": 0,
                "items_mismatch": 0,
                "items_no_price_in_fire": 0,
                "items_no_order_price": 0,
            },
        },
    }

    if env is not None:
        from app.persistence import environments_repo  # avoid import cycle

        fb_cfg = environments_repo.to_fb_config(env)
        if not fb_cfg.get("path"):
            return unavailable

        def open_conn():
            return conn_mgr.connect_with_config(fb_cfg)
    elif conn_mgr.is_configured():
        open_conn = conn_mgr.connect
    else:
        return unavailable

    # Chave exata (spec §2): chaves normalizadas do item e vínculos v2 no escopo
    # dele, resolvidos antes de abrir o Fire (SQLite + intercompany).
    chave_codigos = [
        produto_vinculo_repo.norm_chave("codigo", it.product_code) or None for it in order.items
    ]
    chave_eans = [produto_vinculo_repo.norm_chave("ean", it.ean) or None for it in order.items]
    vinculos, vinculos_completos = _vinculos_v2(order, env, chave_codigos, chave_eans)

    try:
        with open_conn() as conn:
            cur = conn.cursor()

            # Client lookup (inalterado)
            digits = _cnpj_digits(order.header.customer_cnpj)
            client_id: int | None = None
            razao: str | None = None
            if digits:
                cur.execute(queries.FIND_CLIENT_BY_CNPJ, (digits,))
                row = cur.fetchone()
                if row:
                    client_id = row[0]
                    razao = row[1]

            # Coleta chaves (dedup) e resolve em lote
            eans = list({it.ean for it in order.items if it.ean})
            codes = list({it.product_code for it in order.items if it.product_code})
            ean_map = (
                _fetch_map_by_key(cur, queries.find_products_by_eans_sql, eans) if eans else {}
            )
            code_map = (
                _fetch_map_by_key(cur, queries.find_products_by_codes_sql, codes) if codes else {}
            )

            # 3º degrau: de-para por cliente (SQLite local) → resolve SEQ no Fire.
            depara_map: dict[tuple[str, str], dict] = {}
            seq_map: dict[Any, tuple] = {}
            unmatched_codes = [
                it.product_code
                for it in order.items
                if it.product_code
                and it.product_code not in code_map
                and not (it.ean and it.ean in ean_map)
            ]
            unmatched_eans = [it.ean for it in order.items if it.ean and it.ean not in ean_map]
            if unmatched_codes or unmatched_eans:
                try:
                    with db.connect() as sconn:
                        ckey = produto_depara_repo.client_key(
                            order.header.customer_cnpj, order.header.customer_name
                        )
                        depara_map = produto_depara_repo.lookup(
                            sconn,
                            ckey,
                            codigos=unmatched_codes,
                            eans=unmatched_eans,
                        )
                except Exception as exc:  # noqa: BLE001 — sem ambiente ativo / db off: pula degrau
                    from app.utils.logger import logger

                    logger.debug(f"de-para lookup pulado: {type(exc).__name__}: {exc}")
                seqs = list(
                    {
                        int(v["fire_produto_id"])
                        for v in depara_map.values()
                        if str(v["fire_produto_id"]).isdigit()
                    }
                )
                # find_products_by_seqs_sql retorna (SEQ, DESCRICAO, PRECO_VENDA, UNIDADE)
                # — a chave É a 1ª coluna. Não dá pra reusar _fetch_map_by_key
                # (que assume chave + (seq, desc, preco, unidade) separados).
                for chunk in _chunked(seqs):
                    cur.execute(queries.find_products_by_seqs_sql(len(chunk)), tuple(chunk))
                    for row in cur.fetchall():
                        seq_map[row[0]] = (row[0], row[1], row[2], row[3])

            items_report: list[dict] = []
            # Código que vai na coluna CODIGO_PRODUTO do XLSX, por item: o do
            # parser, ou o `fire_codigo` quando o de-para reescreve no export.
            codigos_xlsx: list[str | None] = []
            matched = 0
            price_match = price_mismatch = price_no_price_in_fire = price_no_order_price = 0

            for it in order.items:
                entry = _empty_item_result(it.product_code, it.ean, it.unit_price)
                codigo_xlsx = it.product_code
                hit = None
                source = None
                if it.ean and it.ean in ean_map:
                    hit, source = ean_map[it.ean], "ean"
                elif it.product_code and it.product_code in code_map:
                    hit, source = code_map[it.product_code], "codprod_altern"

                if hit is None:
                    dk = None
                    if it.product_code:
                        dk = ("codigo", _depara_norm_key("codigo", it.product_code))
                    if (dk is None or dk not in depara_map) and it.ean:
                        dk = ("ean", _depara_norm_key("ean", it.ean))
                    dv = depara_map.get(dk) if dk else None
                    if dv is not None:
                        seq = (
                            int(dv["fire_produto_id"])
                            if str(dv["fire_produto_id"]).isdigit()
                            else None
                        )
                        resolved = seq_map.get(seq) if seq is not None else None
                        if resolved is not None:
                            hit, source = resolved, "depara"
                            codigo_xlsx = dv.get("fire_codigo") or it.product_code

                if hit is not None:
                    seq, desc, preco, unidade = hit
                    entry.update(
                        {
                            "match": True,
                            "match_source": source,
                            "fire_product_id": seq,
                            "fire_description": desc,
                            "fire_preco_venda": float(preco) if preco is not None else None,
                            "fire_unidade": (unidade or "").strip() or None,
                        }
                    )
                    matched += 1
                    status = _classify_price(it.unit_price, entry["fire_preco_venda"])
                    entry["price_status"] = status
                    if status == "match":
                        price_match += 1
                    elif status == "mismatch":
                        price_mismatch += 1
                    elif status == "no_price_in_fire":
                        price_no_price_in_fire += 1
                    elif status == "no_order_price":
                        price_no_order_price += 1
                    fire_p = entry["fire_preco_venda"]
                    if fire_p is not None and it.unit_price is not None:
                        entry["price_diff"] = round(float(fire_p) - float(it.unit_price), 2)
                # else: price_status fica 'no_product_match' (default), price_diff None
                items_report.append(entry)
                codigos_xlsx.append(codigo_xlsx)

            trocas, troca_checked = _marcar_trocas_no_fire(cur, items_report, codigos_xlsx)

            # Decisão por chave exata (observada: não bloqueia nada aqui). Falha
            # nesta passada não derruba os campos legados: o item fica
            # fire_indisponivel, que nunca vale como produto conferido.
            try:
                produtos = _decidir_itens(cur, order, chave_codigos, chave_eans, vinculos)
            except Exception as exc:  # noqa: BLE001
                from app.utils.logger import logger

                logger.warning(f"Busca por chave exata falhou ({type(exc).__name__}): {exc}")
                produtos = [_produto_indisponivel() for _ in order.items]
            for entry, produto in zip(items_report, produtos, strict=True):
                entry["produto"] = produto

            cur.close()
    except Exception as exc:  # noqa: BLE001 — any Firebird failure downgrades to "check unavailable"
        from app.utils.logger import logger

        logger.warning(
            f"Product check falhou ({type(exc).__name__}): {exc} — preview segue sem match"
        )
        out = dict(unavailable)
        out["reason"] = f"CHECK_FAILED: {type(exc).__name__}"
        return out

    return {
        "available": True,
        "reason": None,
        "produto_conferencia_incompleta": not vinculos_completos,
        "client": {
            "match": client_id is not None,
            "fire_id": client_id,
            "razao_social": razao,
            "cnpj": order.header.customer_cnpj,
        },
        "items": items_report,
        "summary": {
            "items_total": len(order.items),
            "items_matched": matched,
            "items_missing": len(order.items) - matched,
            "client_matched": client_id is not None,
            "items_troca_no_fire": trocas,
            "troca_no_fire_checked": troca_checked,
            "items_produto_pendente": sum(
                1 for e in items_report if e["produto"]["estado"] != "ok"
            ),
            "produto_conferencia_incompleta": not vinculos_completos,
            "price_summary": {
                "items_match": price_match,
                "items_mismatch": price_mismatch,
                "items_no_price_in_fire": price_no_price_in_fire,
                "items_no_order_price": price_no_order_price,
            },
        },
    }


def _marcar_trocas_no_fire(
    cur, items_report: list[dict], codigos_xlsx: list[str | None]
) -> tuple[int, bool]:
    """Preenche `troca_no_fire` onde o palpite do Fire difere do match do portal.

    Cobre o item sem match também: o Fire não recusa código inexistente se algum
    código começa com ele, e grava o outro em silêncio (pedido 4932). Só avisa,
    nunca bloqueia: o XLSX está certo, o erro acontece dentro do Fire. Qualquer
    falha aqui não derruba o check — devolve (0, False) e o preview segue.
    """
    try:
        palpites = _prever_import_fire(cur, list({c for c in codigos_xlsx if c}))
    except Exception as exc:  # noqa: BLE001 — previsão é extra, o check não depende dela
        from app.utils.logger import logger

        logger.warning(f"Previsão do importador do Fire falhou ({type(exc).__name__}): {exc}")
        return 0, False

    trocas = 0
    for entry, codigo in zip(items_report, codigos_xlsx, strict=True):
        palpite = palpites.get(codigo) if codigo else None
        if palpite is None or palpite[0] == entry["fire_product_id"]:
            continue
        seq, codprod, desc = palpite
        entry["troca_no_fire"] = {
            "fire_product_id": seq,
            "codigo": codprod,
            "descricao": desc,
            # Medido na Fire viva só para item casado por CODPROD_ALTERN ou sem
            # match. Casado por EAN (o XLSX leva a coluna EAN) ou por vínculo
            # (código = SEQ em texto), não se sabe se o importador usa outra
            # chave antes do prefixo — a UI pede conferência, não afirma troca.
            "conferido": entry["match_source"] in (None, "codprod_altern"),
        }
        trocas += 1
    return trocas, True


def is_blocking(
    check: dict,
    ack_items: list[dict] | None = None,
) -> tuple[bool, dict]:
    """Decide se o estado do check impede envio.

    Bloqueia se:
      - Algum item com price_status='mismatch'
      - Algum item com price_status='no_order_price'
      - Algum item com price_status='no_price_in_fire' não coberto por ack_items

    `ack_items`: lista [{ean, product_code, ...}] vinda de
    imports.sem_preco_ack_items. Item é considerado coberto se EAN bate
    (quando ambos presentes) OU product_code bate.

    Quando check['available'] é False, devolve (False, ...) — best-effort:
    sem dados pra avaliar, não bloqueia.

    Retorna (blocked, detail) onde detail = {
      "items_mismatch": [{ean, product_code, order_price, fire_price}],
      "items_no_order_price": [{ean, product_code}],
      "items_no_price_unacked": [{ean, product_code}],
    }.
    """
    detail: dict[str, list] = {
        "items_mismatch": [],
        "items_no_order_price": [],
        "items_no_price_unacked": [],
    }
    if not check.get("available"):
        return False, detail

    ack = ack_items or []
    ack_eans = {a.get("ean") for a in ack if a.get("ean")}
    ack_codes = {a.get("product_code") for a in ack if a.get("product_code")}

    def _covered(item: dict) -> bool:
        if item.get("ean") and item["ean"] in ack_eans:
            return True
        if item.get("product_code") and item["product_code"] in ack_codes:
            return True
        return False

    for it in check.get("items", []):
        status = it.get("price_status")
        if status == "mismatch":
            detail["items_mismatch"].append(
                {
                    "ean": it.get("ean"),
                    "product_code": it.get("product_code"),
                    "order_price": it.get("unit_price_order"),
                    "fire_price": it.get("fire_preco_venda"),
                }
            )
        elif status == "no_order_price":
            detail["items_no_order_price"].append(
                {
                    "ean": it.get("ean"),
                    "product_code": it.get("product_code"),
                }
            )
        elif status == "no_price_in_fire" and not _covered(it):
            detail["items_no_price_unacked"].append(
                {
                    "ean": it.get("ean"),
                    "product_code": it.get("product_code"),
                }
            )

    blocked = bool(
        detail["items_mismatch"]
        or detail["items_no_order_price"]
        or detail["items_no_price_unacked"]
    )
    return blocked, detail
