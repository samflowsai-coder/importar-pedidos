from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.erp_eventos.modelo import ItemErp, PedidoErp, de_estado, para_estado
from app.erp_eventos.mudancas import calcular_mudancas


def item(codigo: int, qtd="10", fat="0", preco="3.15", cod_nf=None) -> ItemErp:
    return ItemErp(
        erp_item_codigo=codigo,
        cod_produto=4410,
        produto_produtor=None,
        descricao="MEIA X",
        qtd=Decimal(qtd),
        qtd_faturada=Decimal(fat),
        cod_nf=cod_nf,
        preco_unitario=Decimal(preco) if preco is not None else None,
        unid="PR",
        dt_entrega_item=None,
    )


def pedido(*itens: ItemErp, status="PEDIDO", dt=date(2026, 11, 5)) -> PedidoErp:
    return PedidoErp(
        erp_codigo=88213,
        pedido_cliente="PC-4471",
        cliente_erp_codigo=512,
        cliente_erp_cnpj="11222333000144",
        cliente_real_cnpj="11222333000144",
        status=status,
        data_pedido=date(2026, 10, 1),
        dt_entrega=dt,
        itens=tuple(itens),
    )


def tipos(ms):
    return [(m.tipo, m.item.erp_item_codigo if m.item else None) for m in ms]


def test_pedido_novo_gera_visto_e_itens_em_ordem_de_codigo():
    ms = calcular_mudancas(None, anterior_excluido=False, atual=pedido(item(3), item(1)))
    assert tipos(ms) == [("pedido_visto", None), ("item_incluido", 1), ("item_incluido", 3)]


def test_sem_mudanca_nao_gera_nada():
    p = pedido(item(1))
    assert calcular_mudancas(p, anterior_excluido=False, atual=p) == []


def test_ordem_quantidade_preco_faturamento_e_status_por_ultimo_no_encerramento():
    antes = pedido(item(1), item(2))
    depois = pedido(
        item(1, qtd="12", preco="3.20", fat="12", cod_nf=5512),
        item(2, fat="10", cod_nf=5512),
        status="FATURADO",
    )
    ms = calcular_mudancas(antes, anterior_excluido=False, atual=depois)
    assert [m.tipo for m in ms] == [
        "quantidade_alterada",
        "preco_alterado",
        "faturamento_detectado",
        "faturamento_detectado",
        "status_alterado",
    ]
    assert ms[2].antes == Decimal("0") and ms[2].depois == Decimal("12") and ms[2].cod_nf == 5512


def test_reabertura_vem_primeiro():
    antes = pedido(item(1), status="CANCELADO")
    depois = pedido(item(1, qtd="20"), status="PEDIDO")
    ms = calcular_mudancas(antes, anterior_excluido=False, atual=depois)
    assert [m.tipo for m in ms] == ["status_alterado", "quantidade_alterada"]
    assert (ms[0].antes, ms[0].depois) == ("CANCELADO", "PEDIDO")


def test_item_removido_e_incluido():
    ms = calcular_mudancas(
        pedido(item(1), item(2)), anterior_excluido=False, atual=pedido(item(2), item(5))
    )
    assert tipos(ms) == [("item_removido", 1), ("item_incluido", 5)]


def test_data_de_entrega():
    ms = calcular_mudancas(
        pedido(item(1)), anterior_excluido=False, atual=pedido(item(1), dt=date(2026, 12, 1))
    )
    assert [(m.tipo, m.antes, m.depois) for m in ms] == [
        ("data_entrega_alterada", date(2026, 11, 5), date(2026, 12, 1))
    ]


def test_excluido_e_reaparecido():
    p = pedido(item(1))
    assert [m.tipo for m in calcular_mudancas(p, anterior_excluido=False, atual=None)] == [
        "pedido_excluido"
    ]
    assert calcular_mudancas(p, anterior_excluido=True, atual=None) == []
    assert tipos(calcular_mudancas(p, anterior_excluido=True, atual=p)) == [
        ("pedido_visto", None),
        ("item_incluido", 1),
    ]


def test_faturamento_estornado_tambem_e_mudanca_com_cod_nf_nulo_quando_nao_resta_nota():
    ms = calcular_mudancas(
        pedido(item(1, fat="5", cod_nf=10)), anterior_excluido=False, atual=pedido(item(1, fat="0"))
    )
    assert [(m.tipo, m.depois, m.cod_nf) for m in ms] == [
        ("faturamento_detectado", Decimal("0"), None)
    ]


def test_estado_ida_e_volta_exata():
    p = pedido(item(2, qtd="1200.0000", preco=None), item(1, qtd="3.5"))
    assert de_estado(para_estado(p)) == pedido(item(1, qtd="3.5"), item(2, qtd="1200", preco=None))
