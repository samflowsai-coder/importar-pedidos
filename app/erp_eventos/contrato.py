"""Espelho, em pydantic estrito, do Zod do Flow (`src/lib/services/erp-eventos/contrato.ts`).

Todo evento e toda página de foto passa por aqui ANTES de entrar na fila: o
que o Flow rejeitaria não sai do Portal. Mudou o Zod do lado de lá, muda aqui
(os casos de `tests/test_erp_eventos_eventos.py` cobrem os do `contrato.test.ts`).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

BASE_ERP_REGEX = r"^[a-z][a-z0-9_]{0,31}$"
_DATA = r"^\d{4}-\d{2}-\d{2}$"
_INSTANTE = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"

Inteiro = Annotated[int, Field(strict=True, ge=0)]
Numero = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Cnpj = Annotated[str, Field(pattern=r"^\d{14}$")]
DataCivil = Annotated[str, Field(pattern=_DATA)]
Instante = Annotated[str, Field(pattern=_INSTANTE)]


class ContratoInvalidoError(ValueError):
    """Evento ou página que o Flow recusaria. Nunca entra na fila."""


class _Estrito(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PedidoContrato(_Estrito):
    erpCodigo: Inteiro
    pedidoCliente: Annotated[str, Field(max_length=120)] | None
    clienteErpCodigo: Inteiro
    clienteErpCnpj: Cnpj | None
    clienteRealCnpj: Cnpj | None
    status: Annotated[str, Field(min_length=1, max_length=40)]
    dataPedido: DataCivil | None
    dtEntrega: DataCivil | None


class ItemContrato(_Estrito):
    erpItemCodigo: Inteiro
    produtoProdutor: Inteiro | None
    codProduto: Inteiro | None
    descricao: Annotated[str, Field(max_length=500)]
    qtd: Numero
    qtdFaturada: Numero
    precoUnitario: Numero | None
    unid: Annotated[str, Field(max_length=10)] | None
    dtEntregaItem: DataCivil | None


class _Comum(_Estrito):
    id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    base: Annotated[str, Field(pattern=BASE_ERP_REGEX)]
    observadoEm: Instante
    importId: Annotated[str, Field(min_length=1, max_length=128)] | None
    indiceOriginal: Inteiro | None
    correlacaoId: Annotated[str, Field(min_length=1, max_length=128)] | None


class _ComPedido(_Comum):
    seq: Annotated[int, Field(strict=True, ge=1)]
    pedido: PedidoContrato


class _ComItem(_ComPedido):
    item: ItemContrato


class PedidoVisto(_ComPedido):
    tipo: Literal["pedido_visto"]


class PedidoExcluido(_ComPedido):
    tipo: Literal["pedido_excluido"]


class ItemIncluido(_ComItem):
    tipo: Literal["item_incluido"]


class ItemRemovido(_ComItem):
    tipo: Literal["item_removido"]


class QuantidadeAlterada(_ComItem):
    tipo: Literal["quantidade_alterada"]
    campo: Literal["qtd"]
    antes: Numero
    depois: Numero


class PrecoAlterado(_ComItem):
    tipo: Literal["preco_alterado"]
    campo: Literal["precoUnitario"]
    antes: Numero | None
    depois: Numero | None


class DataEntregaAlterada(_ComPedido):
    tipo: Literal["data_entrega_alterada"]
    campo: Literal["dtEntrega"]
    antes: DataCivil | None
    depois: DataCivil | None


class StatusAlterado(_ComPedido):
    tipo: Literal["status_alterado"]
    campo: Literal["status"]
    antes: Annotated[str, Field(min_length=1, max_length=40)]
    depois: Annotated[str, Field(min_length=1, max_length=40)]


class FaturamentoDetectado(_ComItem):
    tipo: Literal["faturamento_detectado"]
    campo: Literal["qtdFaturada"]
    antes: Numero
    depois: Numero
    codNf: Inteiro | None


class PernaContrato(_Estrito):
    baseOrigem: Annotated[str, Field(pattern=BASE_ERP_REGEX)]
    erpCodigoOrigem: Inteiro
    erpItemCodigoOrigem: Inteiro
    produtoProdutor: Inteiro
    quantidade: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    linhaLote: Inteiro


class LoteFiscal(_Comum):
    tipo: Literal["lote_fiscal"]
    seq: None
    chaveLote: Annotated[str, Field(min_length=1, max_length=64)]
    erpCodigoEspelho: Inteiro
    pernas: Annotated[list[PernaContrato], Field(min_length=1, max_length=5000)]


_POR_TIPO: dict[str, type[_Comum]] = {
    m.model_fields["tipo"].annotation.__args__[0]: m  # type: ignore[union-attr]
    for m in (
        PedidoVisto,
        PedidoExcluido,
        ItemIncluido,
        ItemRemovido,
        QuantidadeAlterada,
        PrecoAlterado,
        DataEntregaAlterada,
        StatusAlterado,
        FaturamentoDetectado,
        LoteFiscal,
    )
}


class PaginaFoto(_Estrito):
    schema_: Literal["pedido.snapshot_erp.v1"] = Field(alias="schema")
    base: Annotated[str, Field(pattern=BASE_ERP_REGEX)]
    fotoId: Annotated[
        str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    ]
    corteEm: Instante
    pagina: Annotated[int, Field(strict=True, ge=1)]
    totalPaginas: Annotated[int, Field(strict=True, ge=1, le=1000)]
    fim: bool
    pedidos: Annotated[list[_PedidoComItens], Field(max_length=200)]

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    @model_validator(mode="after")
    def _pagina_no_intervalo(self) -> PaginaFoto:
        if self.pagina > self.totalPaginas:
            raise ValueError("pagina > totalPaginas")
        return self


class _PedidoComItens(_Estrito):
    pedido: PedidoContrato
    itens: Annotated[list[ItemContrato], Field(max_length=2000)]


PaginaFoto.model_rebuild()


def validar_evento(payload: dict) -> None:
    modelo = _POR_TIPO.get(payload.get("tipo"))  # type: ignore[arg-type]
    if modelo is None:
        raise ContratoInvalidoError(f"tipo fora do contrato: {payload.get('tipo')!r}")
    try:
        modelo.model_validate(payload)
    except ValidationError as exc:
        raise ContratoInvalidoError(_resumo(exc)) from exc


def validar_pagina(payload: dict) -> None:
    try:
        PaginaFoto.model_validate(payload)
    except ValidationError as exc:
        raise ContratoInvalidoError(_resumo(exc)) from exc


def _resumo(exc: ValidationError) -> str:
    e = exc.errors()[0]
    return f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
