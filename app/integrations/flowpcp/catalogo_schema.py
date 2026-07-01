from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CatalogoProdutoItem(BaseModel):
    """Item de identidade do produto (Fire é dono). camelCase no wire."""

    model_config = ConfigDict(populate_by_name=True)

    fireProdutoId: str = Field(alias="fireProdutoId")  # noqa: N815 — PK imutável do Fire (SEQ)
    codigo: str | None = (
        None  # str(SEQ) — o sequencial que o cliente usa (== fireProdutoId). CODPROD_ALTERN NÃO é usado.
    )
    nome: str
    unidade: str | None = None
    ean: str | None = None
    ativo: bool
    tipo: str | None = (
        None  # 'kit' | 'simples' — derivado de PRODUTOS_KIT (CODTIPOPROD do Fire não é usado).
    )


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


class CatalogoContagens(BaseModel):
    """Buckets do diff Fire×Flow. Wire é camelCase; atributos snake via alias."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    flow_total: int = Field(default=0, alias="flowTotal")
    fire_total: int = Field(default=0, alias="fireTotal")
    match_limpo: int = Field(default=0, alias="matchLimpo")
    ambiguo: int = 0
    flow_only: int = Field(default=0, alias="flowOnly")
    fire_only: int = Field(default=0, alias="fireOnly")


class CatalogoAmostras(BaseModel):
    """Amostras de IDs por bucket (inspeção)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    ambiguo: list[str] = Field(default_factory=list)
    flow_only: list[str] = Field(default_factory=list, alias="flowOnly")
    fire_only: list[str] = Field(default_factory=list, alias="fireOnly")


class CatalogoReconciliacaoResponse(BaseModel):
    """Relatório devolvido pelo Flow (dry-run ou apply). O Flow é dono do
    contrato: `contagens`/`amostras` aninhados (camelCase), `firePkPresente`
    string ('todos'/'parcial'/...). `extra="allow"` tolera campos novos do Flow
    (ex.: contadores de promote na Fase 1)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    dry_run: bool | None = Field(default=None, alias="dryRun")
    full_sync: bool | None = Field(default=None, alias="fullSync")
    fire_pk_presente: str | None = Field(default=None, alias="firePkPresente")
    contagens: CatalogoContagens = Field(default_factory=CatalogoContagens)
    amostras: CatalogoAmostras = Field(default_factory=CatalogoAmostras)
