"""Diff puro: estado anterior × leitura atual → mudanças, em ordem determinística.

Ordem (o Flow aplica por `seq`, então a ordem aqui é a ordem de aplicação):
1. pedido novo (ou que reapareceu depois de excluído): `pedido_visto` e um
   `item_incluido` por item, por `erp_item_codigo` crescente;
2. reabertura (encerrado → aberto): `status_alterado` PRIMEIRO, para o Flow
   reabrir antes de receber mudança de item;
3. `data_entrega_alterada`;
4. itens removidos, incluídos e, nos comuns, quantidade → preço → faturamento
   (cada grupo por `erp_item_codigo` crescente);
5. encerramento (aberto → FATURADO/CANCELADO) ou mudança entre abertos:
   `status_alterado` POR ÚLTIMO, depois do último faturamento;
6. pedido que sumiu do ERP: só `pedido_excluido`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.erp_eventos.modelo import ItemErp, PedidoErp, esta_aberto


@dataclass(frozen=True)
class Mudanca:
    tipo: str
    item: ItemErp | None = None
    campo: str | None = None
    antes: object = None
    depois: object = None
    cod_nf: int | None = None


def _visto(atual: PedidoErp) -> list[Mudanca]:
    return [Mudanca("pedido_visto")] + [
        Mudanca("item_incluido", item=i)
        for i in sorted(atual.itens, key=lambda i: i.erp_item_codigo)
    ]


def calcular_mudancas(
    anterior: PedidoErp | None, *, anterior_excluido: bool, atual: PedidoErp | None
) -> list[Mudanca]:
    if atual is None:
        if anterior is None or anterior_excluido:
            return []
        return [Mudanca("pedido_excluido")]

    if anterior is None or anterior_excluido:
        return _visto(atual)

    saida: list[Mudanca] = []
    status_mudou = anterior.status != atual.status
    reabriu = status_mudou and esta_aberto(atual.status) and not esta_aberto(anterior.status)
    status = Mudanca("status_alterado", campo="status", antes=anterior.status, depois=atual.status)

    if reabriu:
        saida.append(status)

    if anterior.dt_entrega != atual.dt_entrega:
        saida.append(
            Mudanca(
                "data_entrega_alterada",
                campo="dtEntrega",
                antes=anterior.dt_entrega,
                depois=atual.dt_entrega,
            )
        )

    antes_por_codigo = {i.erp_item_codigo: i for i in anterior.itens}
    atual_por_codigo = {i.erp_item_codigo: i for i in atual.itens}

    for codigo in sorted(antes_por_codigo.keys() - atual_por_codigo.keys()):
        saida.append(Mudanca("item_removido", item=antes_por_codigo[codigo]))
    for codigo in sorted(atual_por_codigo.keys() - antes_por_codigo.keys()):
        saida.append(Mudanca("item_incluido", item=atual_por_codigo[codigo]))

    for codigo in sorted(antes_por_codigo.keys() & atual_por_codigo.keys()):
        a, b = antes_por_codigo[codigo], atual_por_codigo[codigo]
        if a.qtd != b.qtd:
            saida.append(
                Mudanca("quantidade_alterada", item=b, campo="qtd", antes=a.qtd, depois=b.qtd)
            )
        if a.preco_unitario != b.preco_unitario:
            saida.append(
                Mudanca(
                    "preco_alterado",
                    item=b,
                    campo="precoUnitario",
                    antes=a.preco_unitario,
                    depois=b.preco_unitario,
                )
            )
        if a.qtd_faturada != b.qtd_faturada:
            saida.append(
                Mudanca(
                    "faturamento_detectado",
                    item=b,
                    campo="qtdFaturada",
                    antes=a.qtd_faturada,
                    depois=b.qtd_faturada,
                    cod_nf=b.cod_nf,
                )
            )

    if status_mudou and not reabriu:
        saida.append(status)
    return saida
