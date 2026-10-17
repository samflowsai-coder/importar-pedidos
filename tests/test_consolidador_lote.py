from __future__ import annotations

from decimal import Decimal

import pytest

from app.consolidators.lote import PernaDoLote, consolidar, preco_espelho
from app.models.order import Order, OrderHeader, OrderItem


def _perna(codigo, itens, numero="X", imp=None):
    return PernaDoLote(
        import_id=imp if imp is not None else f"imp-{codigo}",
        order=Order(
            header=OrderHeader(
                order_number=numero,
                customer_name="C",
                customer_cnpj="1",
                issue_date="24/08/2026",
            ),
            items=itens,
        ),
        fire_codigo_origem=codigo,
    )


def _item(desc, qtd, preco, entrega="21/08/2026", code="P1"):
    return OrderItem(
        description=desc,
        product_code=code,
        quantity=qtd,
        unit_price=preco,
        delivery_date=entrega,
    )


# -- preco ------------------------------------------------------------------


def test_modo_divisor_reproduz_o_que_esta_no_fire():
    """Pedido 4676 real: 16,12 na Nasmar virou 15,0654205607477 na MM."""
    assert preco_espelho(Decimal("16.12"), fator=Decimal("0.07"), modo="divisor") == Decimal(
        "15.0654"
    )


def test_modo_desconto_e_uma_conta_diferente():
    assert preco_espelho(Decimal("16.12"), fator=Decimal("0.07"), modo="desconto") == Decimal(
        "14.9916"
    )


def test_desconto_e_o_default_escolhido_pelo_rafael():
    assert preco_espelho(Decimal("16.12"), fator=Decimal("0.07"), modo="desconto") == Decimal(
        "14.9916"
    )


def test_fator_zero_espelha_o_preco():
    assert preco_espelho(Decimal("16.12"), fator=Decimal("0"), modo="divisor") == Decimal("16.12")


def test_modo_invalido_levanta():
    with pytest.raises(ValueError):
        preco_espelho(Decimal("1"), fator=Decimal("0.07"), modo="markup")


# -- consolidacao -----------------------------------------------------------


def _consolidado(pernas, fator="0", modo="desconto"):
    return consolidar(
        pernas,
        env_origem_slug="nasmar",
        cnpj_revenda="34513679000134",
        nome_revenda="NASMAR COMERCIO DE ROUPAS LTDA",
        chave_lote="NAS-2026-S34",
        fator_preco=Decimal(fator),
        modo_preco=modo,
    )


def test_soma_quantidade_do_mesmo_produto_mesmo_preco_mesma_data():
    """168 linhas viraram 65 no lote #4619 real, com qtd preservada."""
    c = _consolidado(
        [
            _perna(1157, [_item("MEIA", 100.0, 8.19)]),
            _perna(1158, [_item("MEIA", 200.0, 8.19)]),
        ]
    )
    assert len(c.order.items) == 1
    assert c.order.items[0].quantity == 300.0


def test_precos_diferentes_do_mesmo_produto_nao_se_fundem():
    """Caso Nacional Lojas: um CD a 32,00 e seis a 36,00."""
    c = _consolidado(
        [
            _perna(1, [_item("KIT", 10.0, 32.00)]),
            _perna(2, [_item("KIT", 20.0, 36.00)]),
        ]
    )
    assert len(c.order.items) == 2
    assert sorted(i.quantity for i in c.order.items) == [10.0, 20.0]
    assert sorted(i.unit_price for i in c.order.items) == [32.00, 36.00]


def test_datas_de_entrega_diferentes_nao_se_fundem():
    c = _consolidado(
        [
            _perna(1, [_item("MEIA", 10.0, 8.19, entrega="19/08/2026")]),
            _perna(2, [_item("MEIA", 20.0, 8.19, entrega="27/08/2026")]),
        ]
    )
    assert len(c.order.items) == 2
    assert {i.delivery_date for i in c.order.items} == {"19/08/2026", "27/08/2026"}


def test_cliente_do_lote_e_a_revenda():
    c = _consolidado([_perna(1157, [_item("MEIA", 10.0, 8.19)])])
    assert c.order.header.customer_cnpj == "34513679000134"
    assert c.order.header.customer_name == "NASMAR COMERCIO DE ROUPAS LTDA"


def test_pedido_cliente_do_lote_e_a_chave():
    c = _consolidado([_perna(1157, [_item("MEIA", 10.0, 8.19)])])
    assert c.order.header.order_number == "NAS-2026-S34"
    assert c.order.source_file == "lote:NAS-2026-S34"


def test_lote_vazio_levanta():
    with pytest.raises(ValueError):
        _consolidado([])


def test_fator_aplicado_em_desconto():
    c = _consolidado([_perna(1, [_item("KIT", 300.0, 16.12)])], fator="0.07")
    assert c.order.items[0].unit_price == pytest.approx(14.9916)


def test_composicao_aponta_cada_linha_para_as_origens():
    c = _consolidado(
        [
            _perna(1158, [_item("MEIA", 200.0, 8.19)], imp="b"),
            _perna(
                1157,
                [_item("MEIA", 100.0, 8.19), _item("KIT", 5.0, 30.0, code="P2")],
                imp="a",
            ),
        ]
    )
    assert len(c.order.items) == 2
    meia = c.composicao[0]
    assert {(o.import_id, o.indice, o.quantidade) for o in meia} == {
        ("a", 0, Decimal("100")),
        ("b", 0, Decimal("200")),
    }
    assert [(o.import_id, o.indice) for o in c.composicao[1]] == [("a", 1)]


def test_obs_lista_os_codigos_de_origem_em_ordem():
    c = _consolidado([_perna(1161, [_item("A", 1.0, 1.0)]), _perna(1157, [_item("A", 1.0, 1.0)])])
    assert c.obs == "LOTE NAS-2026-S34 | PEDIDOS NASMAR: 1157, 1161"


def test_espelho_nao_carrega_local_de_entrega():
    it = _item("MEIA", 10.0, 8.19)
    it.delivery_cnpj = "05055599002802"
    it.delivery_name = "LOJA"
    it.delivery_ean = "123"
    it.obs = "x"
    c = _consolidado([_perna(1, [it])])
    i = c.order.items[0]
    assert (i.delivery_cnpj, i.delivery_name, i.delivery_ean, i.obs) == (None, None, None, None)


def test_item_sem_preco_levanta_em_vez_de_chutar():
    with pytest.raises(ValueError, match="sem preço") as e:
        _consolidado([_perna(1, [_item("MEIA", 10.0, None)], imp="zz")])
    assert "zz" in str(e.value) and "0" in str(e.value)


def test_item_com_quantidade_nao_positiva_levanta():
    with pytest.raises(ValueError, match="quantidade"):
        _consolidado([_perna(1, [_item("MEIA", 0.0, 1.0)])])


def test_quantidade_soma_em_decimal_sem_ruido_de_float():
    c = _consolidado([_perna(1, [_item("M", 0.1, 1.0)]), _perna(2, [_item("M", 0.2, 1.0)])])
    assert c.order.items[0].quantity == 0.3


def test_issue_date_e_hoje():
    from datetime import date

    c = _consolidado([_perna(1, [_item("M", 1.0, 1.0)])])
    assert c.order.header.issue_date == date.today().strftime("%d/%m/%Y")
