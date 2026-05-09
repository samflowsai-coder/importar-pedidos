"""Match order items against Fire's product catalog before importing.

Read-only. Uses the same lookups as FirebirdExporter (EAN first, then
CODPROD_ALTERN) plus the client CNPJ resolution. Returns a structured report
the UI can render with green / amber / red indicators.

Graceful when FB_DATABASE is not set: returns a report flagged as unavailable
so the preview still loads.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from app.erp import queries
from app.erp.connection import FirebirdConnection
from app.models.order import Order


def _cnpj_digits(cnpj: Optional[str]) -> str:
    if not cnpj:
        return ""
    return re.sub(r"\D", "", cnpj)


def _to_cents(value: Optional[float]) -> Optional[int]:
    """Converte reais em centavos (int) para comparação sem drift de float."""
    if value is None:
        return None
    return int(round(float(value) * 100))


def _classify_price(unit_price_order: Optional[float], fire_preco_venda: Optional[float]) -> str:
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


def _empty_item_result(product_code: Optional[str], ean: Optional[str], unit_price_order: Optional[float]) -> dict:
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
    }


def check_order(order: Order, *, env: dict | None = None) -> dict:
    """Return match report for the order. Safe to call without Fire configured."""
    conn_mgr = FirebirdConnection()

    unavailable: dict[str, Any] = {
        "available": False,
        "reason": "FB_DATABASE_NOT_SET",
        "client": {"match": False, "fire_id": None, "razao_social": None, "cnpj": order.header.customer_cnpj},
        "items": [
            _empty_item_result(it.product_code, it.ean, it.unit_price)
            for it in order.items
        ],
        "summary": {
            "items_total": len(order.items),
            "items_matched": 0,
            "items_missing": len(order.items),
            "client_matched": False,
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

            # Client lookup
            digits = _cnpj_digits(order.header.customer_cnpj)
            client_id: Optional[int] = None
            razao: Optional[str] = None
            if digits:
                cur.execute(queries.FIND_CLIENT_BY_CNPJ, (digits,))
                row = cur.fetchone()
                if row:
                    client_id = row[0]
                    razao = row[1]

            items_report: list[dict] = []
            matched = 0
            price_match = 0
            price_mismatch = 0
            price_no_price_in_fire = 0
            price_no_order_price = 0

            for it in order.items:
                entry = _empty_item_result(it.product_code, it.ean, it.unit_price)
                if it.ean:
                    cur.execute(queries.FIND_PRODUCT_BY_EAN, (it.ean,))
                    row = cur.fetchone()
                    if row:
                        entry.update({
                            "match": True,
                            "match_source": "ean",
                            "fire_product_id": row[0],
                            "fire_description": row[1],
                            "fire_preco_venda": float(row[2]) if row[2] is not None else None,
                        })
                if not entry["match"] and it.product_code:
                    cur.execute(queries.FIND_PRODUCT_BY_CODE, (it.product_code,))
                    row = cur.fetchone()
                    if row:
                        entry.update({
                            "match": True,
                            "match_source": "codprod_altern",
                            "fire_product_id": row[0],
                            "fire_description": row[1],
                            "fire_preco_venda": float(row[2]) if row[2] is not None else None,
                        })

                if entry["match"]:
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

            cur.close()
    except Exception as exc:  # noqa: BLE001 — any Firebird failure downgrades to "check unavailable"
        from app.utils.logger import logger
        logger.warning(f"Product check falhou ({type(exc).__name__}): {exc} — preview segue sem match")
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
            "price_summary": {
                "items_match": price_match,
                "items_mismatch": price_mismatch,
                "items_no_price_in_fire": price_no_price_in_fire,
                "items_no_order_price": price_no_order_price,
            },
        },
    }
