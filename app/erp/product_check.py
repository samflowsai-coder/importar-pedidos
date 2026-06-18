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


def _empty_item_result(product_code: Optional[str], ean: Optional[str]) -> dict:
    return {
        "product_code": product_code,
        "ean": ean,
        "match": False,
        "match_source": None,
        "fire_product_id": None,
        "fire_description": None,
        "fire_preco_venda": None,
    }


def check_order(order: Order, *, env: dict | None = None) -> dict:
    """Return match report for the order. Safe to call without Fire configured."""
    conn_mgr = FirebirdConnection()

    unavailable: dict[str, Any] = {
        "available": False,
        "reason": "FB_DATABASE_NOT_SET",
        "client": {"match": False, "fire_id": None, "razao_social": None, "cnpj": order.header.customer_cnpj},
        "items": [
            _empty_item_result(it.product_code, it.ean) for it in order.items
        ],
        "summary": {
            "items_total": len(order.items),
            "items_matched": 0,
            "items_missing": len(order.items),
            "client_matched": False,
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
            for it in order.items:
                entry = _empty_item_result(it.product_code, it.ean)
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
        },
    }
