from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class OrderHeader(BaseModel):
    order_number: Optional[str] = None
    issue_date: Optional[str] = None
    customer_name: Optional[str] = None
    customer_cnpj: Optional[str] = None


class OrderItem(BaseModel):
    description: Optional[str] = None
    product_code: Optional[str] = None
    ean: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    total_price: Optional[float] = None
    obs: Optional[str] = None
    delivery_date: Optional[str] = None
    delivery_cnpj: Optional[str] = None
    delivery_name: Optional[str] = None


class Order(BaseModel):
    header: OrderHeader
    items: list[OrderItem]
    source_file: str = ""


class ERPRow(BaseModel):
    pedido: str
    nome_cliente: Optional[str] = None
    cnpj_cliente: Optional[str] = None
    codigo_produto: Optional[str] = None
    ean: Optional[str] = None
    descricao: str
    quantidade: float
    preco_unitario: Optional[float] = None
    valor_total: Optional[float] = None
    obs: Optional[str] = None
    data_entrega: Optional[str] = None
    cnpj_local_entrega: Optional[str] = None
