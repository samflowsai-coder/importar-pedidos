from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.erp.queries import FIND_CLIENT_BY_CNPJ, UPDATE_DT_ENTREGA
from app.utils.logger import logger


def _to_fire_date(new_date_iso: str, timezone: str) -> date:
    dt = datetime.fromisoformat(new_date_iso.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(timezone)).date()


def update_dt_entrega(
    conn,
    *,
    pedido_cliente: str,
    cliente_cnpj: str | None,
    new_date_iso: str,
    timezone: str = "America/Sao_Paulo",
) -> int:
    """Resolve o CNPJ → CADASTRO.CODIGO e atualiza CAB_VENDAS.DT_ENTREGA.
    Devolve rows afetadas (0 = cliente não achado ou pedido não localizado)."""
    fire_date = _to_fire_date(new_date_iso, timezone)
    cnpj_clean = re.sub(r"\D", "", cliente_cnpj or "")
    cur = conn.cursor()
    try:
        cur.execute(FIND_CLIENT_BY_CNPJ, (cnpj_clean,))
        client = cur.fetchone()
        if not client:
            logger.warning(f"fire_update: cliente CNPJ={cnpj_clean} não achado no CADASTRO")
            return 0
        cliente_codigo = client[0]
        cur.execute(UPDATE_DT_ENTREGA, (fire_date, pedido_cliente, cliente_codigo))
        rows = cur.rowcount
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
