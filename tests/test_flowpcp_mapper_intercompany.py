# tests/test_flowpcp_mapper_intercompany.py
from __future__ import annotations

from app.erp.depara_cliente import ResolucaoCliente
from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.models.order import Order, OrderHeader, OrderItem

_NASMAR = "34.513.679/0001-34"


def _order() -> Order:
    return Order(
        header=OrderHeader(
            order_number="AF066",
            customer_name="Nasmar Comercio De Roupas Ltda",
            customer_cnpj=_NASMAR,
        ),
        items=[OrderItem(description="MEIA STZ", quantity=12, product_code="123", ean="789")],
        source_file="pedido.pdf",
    )


def test_sem_resolucao_mantem_payload_de_hoje():
    req = build_recebimento_payload(import_id="imp1", order=_order(), tenant_id="t1")
    assert req.cliente.cnpj == "34513679000134"
    assert req.cliente.nome == "Nasmar Comercio De Roupas Ltda"
    assert req.faturadoPor is None


def test_resolucao_troca_o_cliente_e_guarda_o_faturador():
    res = ResolucaoCliente(True, cnpj="10772208000182", nome="AUTHENTIC FEET LTDA", motivo="ok")
    req = build_recebimento_payload(import_id="imp1", order=_order(), tenant_id="t1", resolucao=res)
    assert req.cliente.cnpj == "10772208000182"
    assert req.cliente.nome == "AUTHENTIC FEET LTDA"
    assert req.faturadoPor is not None
    assert req.faturadoPor.cnpj == "34513679000134"
    assert req.faturadoPor.nome == "Nasmar Comercio De Roupas Ltda"


def test_resolucao_sem_nome_nao_usa_nome_da_revenda():
    # Firebird da revenda com NOME e RAZAO_SOCIAL em branco: `nome=None` mas
    # CNPJ resolvido. O nome da revenda (Nasmar) NUNCA pode ser reaproveitado
    # pareado com o CNPJ do cliente real — isso seria uma identidade mista.
    res = ResolucaoCliente(True, cnpj="10772208000182", nome=None, motivo="ok")
    req = build_recebimento_payload(import_id="imp1", order=_order(), tenant_id="t1", resolucao=res)
    assert req.cliente.cnpj == "10772208000182"
    assert req.cliente.nome != "Nasmar Comercio De Roupas Ltda"
    assert "10772208000182" in req.cliente.nome
    # faturadoPor continua correto: quem faturou É a revenda.
    assert req.faturadoPor.nome == "Nasmar Comercio De Roupas Ltda"
    assert req.faturadoPor.cnpj == "34513679000134"


def test_resolucao_nao_resolvida_nao_troca_nada():
    res = ResolucaoCliente(False, motivo="nao_encontrado")
    req = build_recebimento_payload(import_id="imp1", order=_order(), tenant_id="t1", resolucao=res)
    assert req.cliente.cnpj == "34513679000134"
    assert req.faturadoPor is None


def test_itens_e_fornecedor_ficam_intactos():
    res = ResolucaoCliente(True, cnpj="10772208000182", nome="AUTHENTIC FEET LTDA", motivo="ok")
    req = build_recebimento_payload(import_id="imp1", order=_order(), tenant_id="t1", resolucao=res)
    assert req.fornecedor == "Nasmar Comercio De Roupas Ltda"
    assert req.itens[0].produtoCodigo == "123"
    assert req.itens[0].produtoEan == "789"
    assert req.itens[0].quantidade == 12


def test_order_nao_e_mutado():
    order = _order()
    res = ResolucaoCliente(True, cnpj="10772208000182", nome="AUTHENTIC FEET LTDA", motivo="ok")
    build_recebimento_payload(import_id="imp1", order=order, tenant_id="t1", resolucao=res)
    assert order.header.customer_cnpj == _NASMAR
    assert order.header.customer_name == "Nasmar Comercio De Roupas Ltda"


def test_wire_usa_camelcase():
    res = ResolucaoCliente(True, cnpj="10772208000182", nome="AUTHENTIC FEET LTDA", motivo="ok")
    req = build_recebimento_payload(import_id="imp1", order=_order(), tenant_id="t1", resolucao=res)
    wire = req.model_dump(by_alias=True)
    assert wire["faturadoPor"] == {
        "nome": "Nasmar Comercio De Roupas Ltda",
        "cnpj": "34513679000134",
    }
    assert wire["schema"] == "pedido.recebimento.v1"
