from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CatalogoProdutoItem(BaseModel):
    """Item de identidade do produto (Fire é dono). camelCase no wire."""

    model_config = ConfigDict(populate_by_name=True)

    fireProdutoId: str = Field(alias="fireProdutoId")  # noqa: N815 — PK imutável do Fire (SEQ)
    codigo: str | None = None  # str(SEQ) — o sequencial que o cliente usa (== fireProdutoId). CODPROD_ALTERN NÃO é usado.
    nome: str
    unidade: str | None = None
    ean: str | None = None
    ativo: bool
    tipo: str | None = None  # 'kit' | 'simples' — derivado de PRODUTOS_KIT (CODTIPOPROD do Fire não é usado).


class CatalogoOrigem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    importadorVersao: str  # noqa: N815
    extraidoEm: str  # noqa: N815 — ISO8601


class CatalogoRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(default="catalogo.produtos.v1", alias="schema")
    dryRun: bool  # noqa: N815
    fullSync: bool  # noqa: N815
    itens: list[CatalogoProdutoItem]
    origem: CatalogoOrigem


class CatalogoReconciliacaoResponse(BaseModel):
    """Relatório devolvido pelo Flow (dry-run ou apply). O contrato é dono do
    Flow → tolera campos extras (amostras/buckets) sem quebrar o parse."""

    model_config = ConfigDict(extra="allow")

    match_limpo: int = 0
    ambiguo: int = 0
    flow_only: int = 0
    fire_only: int = 0
    criados: int = 0
    atualizados: int = 0
    inalterados: int = 0
    desativados: int = 0
    erros: int = 0
    fire_pk_presente: bool | None = None
