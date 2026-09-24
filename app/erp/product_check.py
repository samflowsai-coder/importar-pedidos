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

from app.erp import queries
from app.erp.connection import FirebirdConnection
from app.models.order import Order
from app.persistence import db, produto_depara_repo
from app.persistence.produto_depara_repo import _norm_key as _depara_norm_key


def _cnpj_digits(cnpj: str | None) -> str:
    if not cnpj:
        return ""
    return re.sub(r"\D", "", cnpj)


def _chunked(values: list, size: int = 200):
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _fetch_map_by_key(cur, sql_builder, values: list) -> dict:
    """Roda o SELECT batelado (chunk de 200) e devolve {key: (seq, desc, preco)}.

    O SELECT tem a chave como 1ª coluna. Valores deduplicados pelo chamador.
    """
    out: dict = {}
    for chunk in _chunked(values):
        cur.execute(sql_builder(len(chunk)), tuple(chunk))
        for row in cur.fetchall():
            key = row[0]
            # first-wins + ORDER BY SEQ replica o ROWS 1 determinístico do lookup antigo
            out.setdefault(key, (row[1], row[2], row[3]))
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
        "unit_price_order": unit_price_order,
        "price_status": "no_product_match",
        "price_diff": None,
        "troca_no_fire": None,
    }


def check_order(order: Order, *, env: dict | None = None) -> dict:
    """Return match report for the order. Safe to call without Fire configured."""
    conn_mgr = FirebirdConnection()

    unavailable: dict[str, Any] = {
        "available": False,
        "reason": "FB_DATABASE_NOT_SET",
        "client": {
            "match": False,
            "fire_id": None,
            "razao_social": None,
            "cnpj": order.header.customer_cnpj,
        },
        "items": [_empty_item_result(it.product_code, it.ean, it.unit_price) for it in order.items],
        "summary": {
            "items_total": len(order.items),
            "items_matched": 0,
            "items_missing": len(order.items),
            "client_matched": False,
            "items_troca_no_fire": 0,
            "troca_no_fire_checked": False,
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
                # find_products_by_seqs_sql retorna (SEQ, DESCRICAO, PRECO_VENDA) — 3
                # colunas, a chave É a 1ª coluna. Não dá pra reusar _fetch_map_by_key
                # (que assume 4 colunas: chave + (seq, desc, preco) separados).
                for chunk in _chunked(seqs):
                    cur.execute(queries.find_products_by_seqs_sql(len(chunk)), tuple(chunk))
                    for row in cur.fetchall():
                        seq_map[row[0]] = (row[0], row[1], row[2])

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
                    seq, desc, preco = hit
                    entry.update(
                        {
                            "match": True,
                            "match_source": source,
                            "fire_product_id": seq,
                            "fire_description": desc,
                            "fire_preco_venda": float(preco) if preco is not None else None,
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
        entry["troca_no_fire"] = {"fire_product_id": seq, "codigo": codprod, "descricao": desc}
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
