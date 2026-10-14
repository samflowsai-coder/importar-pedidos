from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.erp.queries import (
    CODIGOS_POR_PEDIDO_CLIENTE_E_CLIENTE,
    FIND_CLIENT_BY_CNPJ,
    UPDATE_DT_ENTREGA,
)
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


def codigos_da_chave(conn, *, pedido_cliente: str, cliente_cnpj: str | None) -> list[int]:
    """CAB_VENDAS.CODIGO que a chave do `update_dt_entrega` alcança (só leitura).

    Usado pelo eco do SP1 depois de um UPDATE bem-sucedido: a chave
    (PEDIDO_CLIENTE + CLIENTE) não é única (BACKLOG 1.5), então guardamos
    todos os pedidos tocados para a vigia correlacionar a mudança."""
    cnpj_clean = re.sub(r"\D", "", cliente_cnpj or "")
    cur = conn.cursor()
    try:
        cur.execute(FIND_CLIENT_BY_CNPJ, (cnpj_clean,))
        client = cur.fetchone()
        if not client:
            return []
        cur.execute(CODIGOS_POR_PEDIDO_CLIENTE_E_CLIENTE, (pedido_cliente, client[0]))
        return sorted(int(r[0]) for r in cur.fetchall())
    finally:
        cur.close()


def data_no_fire(new_date_iso: str, timezone: str = "America/Sao_Paulo") -> date:
    """A data civil que `update_dt_entrega` grava para este instante."""
    return _to_fire_date(new_date_iso, timezone)
