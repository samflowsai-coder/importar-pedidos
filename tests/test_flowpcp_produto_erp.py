"""Vínculo forte de produto no /recebimento: `erpProdutoId` = SEQ do Fire por item."""

from __future__ import annotations

from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.integrations.flowpcp.produto_erp import anexar_produto_erp, seqs_por_item
from app.models.order import Order, OrderHeader, OrderItem


def _order(n: int = 3) -> Order:
    return Order(
        header=OrderHeader(
            order_number="TSLJ287",
            issue_date="27/08/2026",
            customer_name="Cliente SA",
            customer_cnpj="12.345.678/0001-95",
        ),
        items=[
            OrderItem(
                description=f"KIT {i}", product_code=f"CLI{i}", quantity=12.0, unit_price=10.0
            )
            for i in range(n)
        ],
    )


def _check_item(seq, *, match=True, produto=None) -> dict:
    item = {"product_code": "x", "match": match, "fire_product_id": seq}
    if produto is not None:
        item["produto"] = produto
    return item


# ── payload ───────────────────────────────────────────────────────────────────


def test_payload_leva_erp_produto_id_quando_o_item_tem():
    o = _order(2)
    o.items[0].erp_produto_id = "3914"
    req = build_recebimento_payload(import_id="imp-1", order=o, tenant_id="t")
    assert [i.erpProdutoId for i in req.itens] == ["3914", None]
    corpo = req.model_dump(by_alias=True)
    assert corpo["itens"][0]["erpProdutoId"] == "3914"
    assert corpo["itens"][1]["erpProdutoId"] is None


def test_produto_codigo_do_cliente_continua_igual():
    """O vínculo forte é ADICIONAL: o que resolve hoje (código do cliente, EAN) não muda."""
    o = _order(1)
    o.items[0].erp_produto_id = "3914"
    req = build_recebimento_payload(import_id="imp-1", order=o, tenant_id="t")
    assert req.itens[0].produtoCodigo == "CLI0"


# ── de onde sai o SEQ ─────────────────────────────────────────────────────────


def test_seq_do_check_quando_casou():
    check = {"items": [_check_item(3914), _check_item(None, match=False), _check_item(3916)]}
    assert seqs_por_item(3, check=check) == {0: "3914", 2: "3916"}


def test_check_com_tamanho_diferente_do_pedido_nao_vale():
    check = {"items": [_check_item(3914)]}
    assert seqs_por_item(3, check=check) == {}


def test_decisao_do_produto_em_modo_ligado_vence_o_id_do_item():
    check = {
        "items": [
            _check_item(111, produto={"estado": "ok", "fire_seq": 222}),
            _check_item(333, produto={"estado": "ambiguo", "fire_seq": None}),
        ]
    }
    # `ambiguo` não decidiu nada: cai no id do item, que só existe se casou único.
    assert seqs_por_item(2, check=check) == {0: "222", 1: "333"}


def test_produtos_por_item_explicito_vence_o_check():
    check = {"items": [_check_item(111), _check_item(222)]}
    assert seqs_por_item(2, produtos_por_item={1: 999}, check=check) == {0: "111", 1: "999"}


def test_sem_nada_devolve_vazio():
    assert seqs_por_item(2) == {}
    assert seqs_por_item(2, check={}) == {}
    assert seqs_por_item(2, check={"items": None}) == {}


def test_id_invalido_no_check_e_ignorado():
    check = {"items": [_check_item("abc"), _check_item(0), _check_item(-5)]}
    assert seqs_por_item(3, check=check) == {}


# ── anexar ao pedido ──────────────────────────────────────────────────────────


def test_anexar_preenche_so_os_itens_com_seq():
    o = _order(3)
    anexar_produto_erp(o, {0: "3914", 2: "3916"})
    assert [i.erp_produto_id for i in o.items] == ["3914", None, "3916"]


def test_anexar_ignora_indice_fora_do_pedido():
    o = _order(1)
    anexar_produto_erp(o, {0: "1", 5: "2"})
    assert o.items[0].erp_produto_id == "1"


def test_campo_novo_nao_entra_no_snapshot_antigo():
    """Snapshot persistido antes do campo existir continua válido."""
    antigo = {
        "header": {"order_number": "X"},
        "items": [{"description": "a", "product_code": "p", "quantity": 1}],
    }
    assert Order.model_validate(antigo).items[0].erp_produto_id is None


# ── ligação no envio (server._pedido_para_o_flow) ─────────────────────────────


def test_pedido_para_o_flow_anexa_o_seq_do_check_salvo_do_import():
    from types import SimpleNamespace

    from app.web import server

    o = _order(2)
    entry = {"check": {"items": [_check_item(3914), _check_item(None, match=False)]}}
    flow = server._pedido_para_o_flow(o, SimpleNamespace(check=None), None, entry)
    assert [i.erp_produto_id for i in flow.items] == ["3914", None]
    assert flow is o  # fora de `ligado` é o próprio pedido, como antes


def test_pedido_para_o_flow_em_ligado_copia_e_nao_suja_o_pedido_do_fire():
    from types import SimpleNamespace

    from app.web import server

    o = _order(1)
    decisao = SimpleNamespace(
        check={"items": [_check_item(7, produto={"estado": "ok", "fire_seq": 8})]}
    )
    flow = server._pedido_para_o_flow(o, decisao, None, None)
    assert flow is not o
    assert flow.items[0].erp_produto_id == "8"
    assert o.items[0].erp_produto_id is None


def test_pedido_para_o_flow_sem_check_nem_mapa_nao_muda_nada():
    from types import SimpleNamespace

    from app.web import server

    o = _order(1)
    flow = server._pedido_para_o_flow(o, SimpleNamespace(check=None), None, {})
    assert flow.items[0].erp_produto_id is None
