"""Tests for app.erp.product_check — match e price_status."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.erp import product_check
from app.models.order import Order, OrderHeader, OrderItem


def _order(items_kwargs: list[dict], *, customer_cnpj: str = "00000000000100") -> Order:
    return Order(
        header=OrderHeader(order_number="T1", customer_cnpj=customer_cnpj, customer_name="ACME"),
        items=[OrderItem(quantity=1.0, **kw) for kw in items_kwargs],
    )


def _make_fb_ctx(*, client_row=None, product_rows: dict | None = None):
    """Cria um context manager fake que devolve cursor com fetchone() programado.

    `product_rows` mapeia (query_str, bind_value) -> tuple|None.
    """
    cur = MagicMock()
    rows_seq: list = []

    def execute_side_effect(sql, params):
        # Decide o próximo fetchone com base no SQL
        if "FROM CADASTRO" in sql:
            rows_seq.append(client_row)
        elif "FROM PRODUTOS" in sql:
            key = ("ean" if "CODIGO_EAN13" in sql else "code", params[0])
            rows_seq.append((product_rows or {}).get(key))
        else:
            rows_seq.append(None)

    cur.execute.side_effect = execute_side_effect
    cur.fetchone.side_effect = lambda: rows_seq.pop(0)

    conn = MagicMock()
    conn.cursor.return_value = cur

    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return ctx


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_match_exact(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        client_row=(1, "ACME"),
        product_rows={("ean", "7891"): (10, "TENIS", 89.90)},
    )

    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)

    item = report["items"][0]
    assert item["price_status"] == "match"
    assert item["unit_price_order"] == 89.90
    assert item["fire_preco_venda"] == 89.90
    assert item["price_diff"] == 0.0


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_mismatch_one_cent(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        product_rows={("ean", "7891"): (10, "TENIS", 89.91)},
    )
    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)
    item = report["items"][0]
    assert item["price_status"] == "mismatch"
    assert item["price_diff"] == 0.01


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_mismatch_round_value(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        product_rows={("ean", "7891"): (10, "TENIS", 100.00)},
    )
    order = _order([{"ean": "7891", "unit_price": 99.00}])
    report = product_check.check_order(order)
    assert report["items"][0]["price_status"] == "mismatch"
    assert report["items"][0]["price_diff"] == 1.0


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_no_price_in_fire_null(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        product_rows={("ean", "7891"): (10, "TENIS", None)},
    )
    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)
    assert report["items"][0]["price_status"] == "no_price_in_fire"
    assert report["items"][0]["fire_preco_venda"] is None


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_no_price_in_fire_zero(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        product_rows={("ean", "7891"): (10, "TENIS", 0.0)},
    )
    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)
    assert report["items"][0]["price_status"] == "no_price_in_fire"


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_no_order_price(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        product_rows={("ean", "7891"): (10, "TENIS", 50.0)},
    )
    order = _order([{"ean": "7891", "unit_price": None}])
    report = product_check.check_order(order)
    assert report["items"][0]["price_status"] == "no_order_price"


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_no_product_match(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        product_rows={},  # nada no Fire
    )
    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)
    item = report["items"][0]
    assert item["match"] is False
    assert item["price_status"] == "no_product_match"
    assert item["price_diff"] is None


@patch("app.erp.product_check.FirebirdConnection")
def test_summary_aggregates_price_counts(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        product_rows={
            ("ean", "A"): (1, "X", 10.0),  # match
            ("ean", "B"): (2, "Y", 12.0),  # mismatch
            ("ean", "C"): (3, "Z", None),  # no_price_in_fire
            ("ean", "D"): (4, "W", 50.0),  # no_order_price
            # E não cadastrado → no_product_match
        },
    )
    order = _order(
        [
            {"ean": "A", "unit_price": 10.0},
            {"ean": "B", "unit_price": 11.0},
            {"ean": "C", "unit_price": 30.0},
            {"ean": "D", "unit_price": None},
            {"ean": "E", "unit_price": 5.0},
        ]
    )
    summary = product_check.check_order(order)["summary"]["price_summary"]
    assert summary == {
        "items_match": 1,
        "items_mismatch": 1,
        "items_no_price_in_fire": 1,
        "items_no_order_price": 1,
    }


# ---------------------------------------------------------------------------
# Task 4 — is_blocking() helper
# ---------------------------------------------------------------------------


def _check_with(items: list[dict]) -> dict:
    return {"available": True, "items": items, "summary": {}}


def test_is_blocking_passes_match_only():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "match"}])
    blocked, detail = product_check.is_blocking(check)
    assert blocked is False
    assert detail["items_mismatch"] == []
    assert detail["items_no_order_price"] == []
    assert detail["items_no_price_unacked"] == []


def test_is_blocking_blocks_on_mismatch():
    check = _check_with(
        [
            {
                "ean": "A",
                "product_code": "p1",
                "price_status": "mismatch",
                "unit_price_order": 11.0,
                "fire_preco_venda": 10.0,
            },
        ]
    )
    blocked, detail = product_check.is_blocking(check)
    assert blocked is True
    assert detail["items_mismatch"] == [
        {"ean": "A", "product_code": "p1", "order_price": 11.0, "fire_price": 10.0},
    ]


def test_is_blocking_blocks_on_no_order_price():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "no_order_price"}])
    blocked, detail = product_check.is_blocking(check)
    assert blocked is True
    assert detail["items_no_order_price"] == [{"ean": "A", "product_code": "p1"}]


def test_is_blocking_blocks_on_no_price_unacked():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "no_price_in_fire"}])
    blocked, detail = product_check.is_blocking(check, ack_items=None)
    assert blocked is True
    assert detail["items_no_price_unacked"] == [{"ean": "A", "product_code": "p1"}]


def test_is_blocking_passes_with_ack_by_ean():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "no_price_in_fire"}])
    blocked, _ = product_check.is_blocking(check, ack_items=[{"ean": "A", "product_code": None}])
    assert blocked is False


def test_is_blocking_passes_with_ack_by_code():
    check = _check_with([{"ean": None, "product_code": "p1", "price_status": "no_price_in_fire"}])
    blocked, _ = product_check.is_blocking(check, ack_items=[{"ean": None, "product_code": "p1"}])
    assert blocked is False


def test_is_blocking_partial_ack_still_blocks():
    check = _check_with(
        [
            {"ean": "A", "product_code": "p1", "price_status": "no_price_in_fire"},
            {"ean": "B", "product_code": "p2", "price_status": "no_price_in_fire"},
        ]
    )
    blocked, detail = product_check.is_blocking(
        check,
        ack_items=[{"ean": "A", "product_code": "p1"}],
    )
    assert blocked is True
    assert detail["items_no_price_unacked"] == [{"ean": "B", "product_code": "p2"}]


def test_is_blocking_ignores_no_product_match():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "no_product_match"}])
    blocked, _ = product_check.is_blocking(check)
    assert blocked is False  # comportamento atual mantido — sem match não bloqueia aqui


def test_is_blocking_returns_false_when_check_unavailable():
    blocked, _ = product_check.is_blocking({"available": False, "items": []})
    assert blocked is False  # check off → segue (best-effort)
