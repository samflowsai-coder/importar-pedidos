"""Pedido que só existe no Fire → payload do /recebimento (tools/flow_enviar_pedidos_fire.py)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.erp_eventos.modelo import ItemErp, PedidoErp
from app.integrations.flowpcp.recebimento_fire import (
    JANELA_VINCULO_DIAS,
    external_id_do_fire,
    montar_recebimento,
    recusa_local,
)

HOJE = date(2026, 10, 22)


def _item(codigo: int, *, produto: int | None = 3914, qtd: str = "12", **kw) -> ItemErp:
    base = dict(
        erp_item_codigo=codigo,
        cod_produto=produto,
        produto_produtor=None,
        descricao=f"KIT CANO CURTO {codigo}",
        qtd=Decimal(qtd),
        qtd_faturada=Decimal(0),
        cod_nf=None,
        preco_unitario=Decimal("39.9"),
        unid="KIT",
        dt_entrega_item=None,
    )
    base.update(kw)
    return ItemErp(**base)


def _pedido(**kw) -> PedidoErp:
    base = dict(
        erp_codigo=4709,
        pedido_cliente="TSLJ287",
        cliente_erp_codigo=77,
        cliente_erp_cnpj="12345678000195",
        cliente_real_cnpj=None,
        status="PEDIDO",
        data_pedido=date(2026, 9, 1),
        dt_entrega=date(2026, 11, 30),
        itens=(_item(40259), _item(40261, produto=3915, qtd="6")),
    )
    base.update(kw)
    return PedidoErp(**base)


# ── externalId ────────────────────────────────────────────────────────────────


def test_external_id_e_deterministico_e_distingue_pedido_e_empresa():
    a = external_id_do_fire(4709, slug="mm")
    assert a == external_id_do_fire(4709, slug="mm")
    assert a != external_id_do_fire(4710, slug="mm")
    assert a != external_id_do_fire(4709, slug="nasmar")
    assert len(a) <= 128


# ── payload ───────────────────────────────────────────────────────────────────


def test_payload_campos_basicos():
    req = montar_recebimento(_pedido(), razao_social="Comercial Nicky's Ltda", slug="mm")
    assert req.externalId == external_id_do_fire(4709, slug="mm")
    assert req.pedidoNumero == "TSLJ287"
    assert req.cliente.cnpj == "12345678000195"
    assert req.cliente.nome == "Comercial Nicky's Ltda"
    assert req.emitidoEm == "2026-09-01T00:00:00.000Z"
    assert req.prazoSolicitado == "2026-11-30T00:00:00.000Z"
    assert [i.quantidade for i in req.itens] == [12.0, 6.0]
    assert [i.precoUnitario for i in req.itens] == [39.9, 39.9]
    assert req.itens[0].descricao == "KIT CANO CURTO 40259"
    assert req.origem.parserUsado == "fire_direto"
    assert req.origem.arquivoOriginal == "fire:4709"
    assert req.faturadoPor is None


def test_prazo_vem_do_primeiro_item_quando_ele_tem_data_propria():
    p = _pedido(itens=(_item(1, dt_entrega_item=date(2026, 12, 5)), _item(2)))
    req = montar_recebimento(p, razao_social="X", slug="mm")
    assert req.prazoSolicitado == "2026-12-05T00:00:00.000Z"


def test_prazo_nulo_quando_nem_item_nem_cabecalho_tem_data():
    p = _pedido(dt_entrega=None)
    req = montar_recebimento(p, razao_social="X", slug="mm")
    assert req.prazoSolicitado is None


def test_cliente_real_tem_precedencia_sobre_o_cnpj_do_erp():
    p = _pedido(cliente_real_cnpj="98765432000110")
    req = montar_recebimento(p, razao_social="X", slug="mm")
    assert req.cliente.cnpj == "98765432000110"


def test_produto_codigo_usa_o_mapa_do_flow_depois_o_seq_do_fire():
    p = _pedido(
        itens=(_item(40259, produto=3914), _item(40261, produto=3915), _item(40262, produto=None))
    )
    req = montar_recebimento(p, razao_social="X", slug="mm", codigos_flow={40259: "KIT-2972"})
    assert [i.produtoCodigo for i in req.itens] == ["KIT-2972", "3915", None]


def test_ordem_dos_itens_segue_o_codigo_do_item_no_fire():
    p = _pedido(itens=(_item(30), _item(10), _item(20)))
    req = montar_recebimento(p, razao_social="X", slug="mm")
    assert [i.descricao for i in req.itens] == [
        "KIT CANO CURTO 10",
        "KIT CANO CURTO 20",
        "KIT CANO CURTO 30",
    ]


def test_payload_passa_no_schema_do_contrato():
    req = montar_recebimento(_pedido(), razao_social="X", slug="mm")
    corpo = req.model_dump(by_alias=True, exclude_none=False)
    assert corpo["schema"] == "pedido.recebimento.v1"
    assert type(req).model_validate(corpo) == req


# ── recusa local ──────────────────────────────────────────────────────────────


def test_pedido_valido_nao_e_recusado():
    assert recusa_local(_pedido(), hoje=HOJE) is None


@pytest.mark.parametrize(
    ("campo", "valor", "trecho"),
    [
        ("status", "FATURADO", "status"),
        ("status", "EM ANÁLISE", "status"),
        ("pedido_cliente", None, "pedido do cliente"),
        ("pedido_cliente", "", "pedido do cliente"),
        ("pedido_cliente", "X" * 65, "pedido do cliente"),
        ("cliente_erp_cnpj", None, "cnpj"),
        ("data_pedido", None, "data do pedido"),
        ("itens", (), "sem itens"),
    ],
)
def test_recusas_de_cabecalho(campo, valor, trecho):
    motivo = recusa_local(replace(_pedido(), **{campo: valor}), hoje=HOJE)
    assert motivo is not None
    assert trecho in motivo.lower()


def test_recusa_pedido_fora_da_janela_do_vinculo():
    # 90 dias é o limite do vínculo no Flow; a margem fica por conta do ciclo do SP1.
    limite = date(2026, 10, 22).toordinal() - JANELA_VINCULO_DIAS
    dentro = _pedido(data_pedido=date.fromordinal(limite))
    fora = _pedido(data_pedido=date.fromordinal(limite - 1))
    assert recusa_local(dentro, hoje=HOJE) is None
    assert "janela" in (recusa_local(fora, hoje=HOJE) or "")


def test_janela_tem_margem_sobre_os_90_dias_do_flow():
    assert JANELA_VINCULO_DIAS < 90


@pytest.mark.parametrize(
    "item",
    [
        _item(1, qtd="0"),
        _item(1, qtd="-3"),
        _item(1, qtd="1.005"),  # o Flow só aceita múltiplo de 0,01
        _item(1, descricao=""),
        _item(1, qtd_faturada=Decimal("2")),  # faturado em parte: o saldo não é o pedido
    ],
)
def test_recusas_de_item(item):
    assert recusa_local(_pedido(itens=(_item(9), item)), hoje=HOJE) is not None


def test_recusa_mais_de_500_itens():
    muitos = tuple(_item(i) for i in range(1, 502))
    assert "500" in (recusa_local(_pedido(itens=muitos), hoje=HOJE) or "")


# ── vínculo forte e janela por data ───────────────────────────────────────────


def test_erp_produto_id_vem_do_codproduto_do_fire():
    p = _pedido(itens=(_item(1, produto=3914), _item(2, produto=None)))
    req = montar_recebimento(p, razao_social="X", slug="mm", codigos_flow={1: "KIT-2972"})
    # O código do Flow só decide o `produtoCodigo`; o vínculo forte é sempre o SEQ do Fire.
    assert [i.erpProdutoId for i in req.itens] == ["3914", None]
    assert req.itens[0].produtoCodigo == "KIT-2972"


def test_desde_mantem_pedido_de_2026_mesmo_fora_da_janela_de_dias():
    antigo = _pedido(data_pedido=date(2026, 3, 24))
    assert "janela" in (recusa_local(antigo, hoje=HOJE) or "")
    assert recusa_local(antigo, hoje=HOJE, desde=date(2026, 1, 1)) is None


def test_desde_ainda_recusa_o_que_e_anterior():
    velho = _pedido(data_pedido=date(2025, 12, 31))
    assert "antes de 2026-01-01" in (recusa_local(velho, hoje=HOJE, desde=date(2026, 1, 1)) or "")
