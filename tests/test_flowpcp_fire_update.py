from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.erp.fire_update import update_dt_entrega


def _conn(client_row, update_rowcount):
    cur = MagicMock()
    cur.fetchone.return_value = client_row  # FIND_CLIENT_BY_CNPJ result
    cur.rowcount = update_rowcount
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def test_update_resolves_cnpj_then_updates_dt_entrega():
    conn, cur = _conn(client_row=(42, "MM AMERICANENSE"), update_rowcount=1)
    rows = update_dt_entrega(
        conn,
        pedido_cliente="AW097",
        cliente_cnpj="12.345.678/0001-90",
        new_date_iso="2026-07-17T03:00:00.000Z",
    )
    assert rows == 1
    conn.commit.assert_called_once()
    # segunda execução (o UPDATE) recebeu o CLIENTE codigo resolvido (42) e o pedido
    update_args = cur.execute.call_args_list[-1].args[1]
    assert 42 in update_args
    assert "AW097" in update_args


def test_returns_zero_when_client_not_found():
    conn, cur = _conn(client_row=None, update_rowcount=0)
    rows = update_dt_entrega(
        conn,
        pedido_cliente="AW097",
        cliente_cnpj="00000000000000",
        new_date_iso="2026-07-17T03:00:00.000Z",
    )
    assert rows == 0
    conn.commit.assert_not_called()


def test_rollback_on_error():
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = (42, "MM")
    cur.execute.side_effect = [None, RuntimeError("lock")]  # SELECT ok, UPDATE falha
    conn.cursor.return_value = cur
    with pytest.raises(RuntimeError):
        update_dt_entrega(
            conn,
            pedido_cliente="AW097",
            cliente_cnpj="123",
            new_date_iso="2026-07-17T03:00:00.000Z",
        )
    conn.rollback.assert_called_once()
