from __future__ import annotations

from pydantic import BaseModel


class OrderHeader(BaseModel):
    order_number: str | None = None
    # True quando o documento não trazia número e o pipeline gerou `SN-<hash>`
    # (ver app/pipeline.py). O número vai pro Fire e pra nota fiscal como se
    # fosse do cliente; a marca é o que deixa o preview avisar isso.
    order_number_gerado: bool = False
    issue_date: str | None = None
    customer_name: str | None = None
    customer_cnpj: str | None = None
    # CNPJ do FORNECEDOR impresso no documento, em dígitos. Preenchido pelo
    # pipeline (varredura) ou por um parser que saiba ler o rótulo. É a chave
    # do degrau 1 do roteamento — ver app/routing/documento.py.
    supplier_cnpj: str | None = None


class OrderItem(BaseModel):
    description: str | None = None
    product_code: str | None = None
    ean: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    total_price: float | None = None
    obs: str | None = None
    delivery_date: str | None = None
    delivery_cnpj: str | None = None
    delivery_name: str | None = None
    delivery_ean: str | None = None


class Order(BaseModel):
    header: OrderHeader
    items: list[OrderItem]
    source_file: str = ""


class ERPRow(BaseModel):
    pedido: str
    nome_cliente: str | None = None
    cnpj_cliente: str | None = None
    codigo_produto: str | None = None
    ean: str | None = None
    descricao: str
    quantidade: float
    preco_unitario: float | None = None
    valor_total: float | None = None
    obs: str | None = None
    data_entrega: str | None = None
    cnpj_local_entrega: str | None = None
    ean_local_entrega: str | None = None
