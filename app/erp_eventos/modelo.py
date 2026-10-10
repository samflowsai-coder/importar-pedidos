"""Pedido do ERP como a vigia o enxerga: leitura do ERP e estado persistido.

`PedidoErp` é o mesmo objeto nos dois papéis — o que a vigia leu agora e o que
ela leu da última vez (`erp_pedido_estado.estado_json`). Ida e volta pelo JSON
é exata (`para_estado`/`de_estado`): números viajam como texto canônico,
nunca como float.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.erp_eventos.canonico import decimal_canonico

STATUS_ENCERRADOS = frozenset({"FATURADO", "CANCELADO"})


def esta_aberto(status: str) -> bool:
    """Mesma regra do Flow (spec SP1 §4.1): qualquer status fora de FATURADO/CANCELADO é aberto."""
    return status.strip().upper() not in STATUS_ENCERRADOS


@dataclass(frozen=True)
class ItemErp:
    erp_item_codigo: int
    cod_produto: int | None
    produto_produtor: int | None
    descricao: str
    qtd: Decimal
    qtd_faturada: Decimal
    cod_nf: int | None
    preco_unitario: Decimal | None
    unid: str | None
    dt_entrega_item: date | None


@dataclass(frozen=True)
class PedidoErp:
    erp_codigo: int
    pedido_cliente: str | None
    cliente_erp_codigo: int
    cliente_erp_cnpj: str | None
    cliente_real_cnpj: str | None
    status: str
    data_pedido: date | None
    dt_entrega: date | None
    itens: tuple[ItemErp, ...]

    def item(self, erp_item_codigo: int) -> ItemErp | None:
        return next((i for i in self.itens if i.erp_item_codigo == erp_item_codigo), None)


def _dec(v: Decimal | None) -> str | None:
    return None if v is None else decimal_canonico(v)


def _data(v: date | None) -> str | None:
    return None if v is None else v.isoformat()


def para_estado(p: PedidoErp) -> dict:
    return {
        "erp_codigo": p.erp_codigo,
        "pedido_cliente": p.pedido_cliente,
        "cliente_erp_codigo": p.cliente_erp_codigo,
        "cliente_erp_cnpj": p.cliente_erp_cnpj,
        "cliente_real_cnpj": p.cliente_real_cnpj,
        "status": p.status,
        "data_pedido": _data(p.data_pedido),
        "dt_entrega": _data(p.dt_entrega),
        "itens": [
            {
                "erp_item_codigo": i.erp_item_codigo,
                "cod_produto": i.cod_produto,
                "produto_produtor": i.produto_produtor,
                "descricao": i.descricao,
                "qtd": _dec(i.qtd),
                "qtd_faturada": _dec(i.qtd_faturada),
                "cod_nf": i.cod_nf,
                "preco_unitario": _dec(i.preco_unitario),
                "unid": i.unid,
                "dt_entrega_item": _data(i.dt_entrega_item),
            }
            for i in sorted(p.itens, key=lambda i: i.erp_item_codigo)
        ],
    }


def de_estado(d: dict) -> PedidoErp:
    def dec(v):
        return None if v is None else Decimal(v)

    def data(v):
        return None if v is None else date.fromisoformat(v)

    return PedidoErp(
        erp_codigo=d["erp_codigo"],
        pedido_cliente=d["pedido_cliente"],
        cliente_erp_codigo=d["cliente_erp_codigo"],
        cliente_erp_cnpj=d["cliente_erp_cnpj"],
        cliente_real_cnpj=d["cliente_real_cnpj"],
        status=d["status"],
        data_pedido=data(d["data_pedido"]),
        dt_entrega=data(d["dt_entrega"]),
        itens=tuple(
            ItemErp(
                erp_item_codigo=i["erp_item_codigo"],
                cod_produto=i["cod_produto"],
                produto_produtor=i["produto_produtor"],
                descricao=i["descricao"],
                qtd=Decimal(i["qtd"]),
                qtd_faturada=Decimal(i["qtd_faturada"]),
                cod_nf=i["cod_nf"],
                preco_unitario=dec(i["preco_unitario"]),
                unid=i["unid"],
                dt_entrega_item=data(i["dt_entrega_item"]),
            )
            for i in d["itens"]
        ),
    )
