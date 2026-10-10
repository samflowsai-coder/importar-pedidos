"""Mudança → evento do contrato v1, com `id`, `seq` e `unidade` de envio.

`unidade` agrupa o que não pode ser separado por um corte de lote: um
`pedido_visto` e os `item_incluido` que o acompanham (spec SP1 §4.1). Todo
outro evento é uma unidade sozinho.

Puro: não lê banco, não sabe de fila. Quem chama já reservou os `seq`
(`primeiro_seq` .. `primeiro_seq + len(mudancas) - 1`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.erp_eventos.canonico import id_evento, numero_json
from app.erp_eventos.contrato import validar_evento
from app.erp_eventos.modelo import ItemErp, PedidoErp
from app.erp_eventos.mudancas import Mudanca


@dataclass(frozen=True)
class Vinculo:
    import_id: str
    indice_original: int


@dataclass(frozen=True)
class EventoFila:
    id: str
    base: str
    erp_codigo: int
    seq: int | None
    tipo: str
    unidade: str
    payload: dict
    observado_em: str


def pedido_json(p: PedidoErp) -> dict:
    return {
        "erpCodigo": p.erp_codigo,
        "pedidoCliente": p.pedido_cliente,
        "clienteErpCodigo": p.cliente_erp_codigo,
        "clienteErpCnpj": p.cliente_erp_cnpj,
        "clienteRealCnpj": p.cliente_real_cnpj,
        "status": p.status,
        "dataPedido": p.data_pedido.isoformat() if p.data_pedido else None,
        "dtEntrega": p.dt_entrega.isoformat() if p.dt_entrega else None,
    }


def item_json(i: ItemErp) -> dict:
    return {
        "erpItemCodigo": i.erp_item_codigo,
        "produtoProdutor": i.produto_produtor,
        "codProduto": i.cod_produto,
        "descricao": i.descricao,
        "qtd": numero_json(i.qtd),
        "qtdFaturada": numero_json(i.qtd_faturada),
        "precoUnitario": numero_json(i.preco_unitario),
        "unid": i.unid,
        "dtEntregaItem": i.dt_entrega_item.isoformat() if i.dt_entrega_item else None,
    }


def _valor_json(v: object) -> object:
    if isinstance(v, Decimal):
        return numero_json(v)
    if isinstance(v, date):
        return v.isoformat()
    return v


def montar_eventos(
    *,
    tenant: str,
    base: str,
    observado_em: str,
    pedido: PedidoErp,
    mudancas: list[Mudanca],
    primeiro_seq: int,
    vinculos: dict[int, Vinculo],
    import_do_pedido: str | None,
    correlacao_data_entrega: str | None = None,
) -> list[EventoFila]:
    """Um evento por mudança, na ordem recebida, com `seq` contíguo.

    Levanta `ContratoInvalidoError` se QUALQUER evento do pedido violar o contrato:
    o pedido inteiro fica de fora desta vigia (nada parcial entra na fila).
    """
    saida: list[EventoFila] = []
    unidade_visto: str | None = None
    for n, m in enumerate(mudancas):
        seq = primeiro_seq + n
        item_codigo = m.item.erp_item_codigo if m.item else None
        vinculo = vinculos.get(item_codigo) if item_codigo is not None else None
        payload: dict = {
            "id": id_evento(
                tenant=tenant,
                base=base,
                tipo=m.tipo,
                erp_codigo=pedido.erp_codigo,
                erp_item_codigo=item_codigo,
                campo=m.campo,
                antes=m.antes,
                depois=m.depois,
                observado_em=observado_em,
            ),
            "base": base,
            "tipo": m.tipo,
            "observadoEm": observado_em,
            "seq": seq,
            "pedido": pedido_json(pedido),
            "importId": vinculo.import_id if vinculo else import_do_pedido,
            "indiceOriginal": vinculo.indice_original if vinculo else None,
            "correlacaoId": correlacao_data_entrega if m.tipo == "data_entrega_alterada" else None,
        }
        if m.item is not None:
            payload["item"] = item_json(m.item)
        if m.campo is not None:
            payload["campo"] = m.campo
            payload["antes"] = _valor_json(m.antes)
            payload["depois"] = _valor_json(m.depois)
        if m.tipo == "faturamento_detectado":
            payload["codNf"] = m.cod_nf
        validar_evento(payload)

        if m.tipo == "pedido_visto":
            unidade_visto = f"{base}:{pedido.erp_codigo}:{seq}"
            unidade = unidade_visto
        elif m.tipo == "item_incluido" and unidade_visto is not None:
            unidade = unidade_visto
        else:
            unidade_visto = None
            unidade = f"{base}:{pedido.erp_codigo}:{seq}"
        saida.append(
            EventoFila(
                id=payload["id"],
                base=base,
                erp_codigo=pedido.erp_codigo,
                seq=seq,
                tipo=m.tipo,
                unidade=unidade,
                payload=payload,
                observado_em=observado_em,
            )
        )
    return saida
