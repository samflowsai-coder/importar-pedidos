from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_serializer


class CatalogoComponente(BaseModel):
    """Um filho da composição (PRODUTOS_KIT). Nome pelo papel (P-PROD-08 do Flow)."""

    model_config = ConfigDict(populate_by_name=True)

    erpProdutoId: str  # noqa: N815 — SEQ do filho
    quantidade: float


class CatalogoGrupoErp(BaseModel):
    codigo: str
    nome: str


def _sem_os_nao_preenchidos(modelo: BaseModel, corpo: dict, campos: tuple[str, ...]) -> dict:
    """Chave só vai no JSON quando foi preenchida: ausente ≠ [] ≠ null (ausente =
    "não mexer" no Flow). Com o interruptor desligado o payload fica igual ao de
    antes da composição."""
    for campo in campos:
        if campo not in modelo.model_fields_set:
            corpo.pop(campo, None)
    return corpo


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
        None  # 'pack' | 'kit' | 'simples' — catalog_extract._tipo (CODTIPOPROD do Fire não é usado).
    )
    componentes: list[CatalogoComponente] | None = None
    codigoMes: str | None = None  # noqa: N815 — CODPROD_ALTERN só dígitos, só em par ativo
    # Fiscais (catalogo_fiscal): só vão no JSON quando preenchidos
    ncm: str | None = None
    tipoItemSped: int | None = None  # noqa: N815
    grupoErp: CatalogoGrupoErp | None = None  # noqa: N815
    codigoNfe: str | None = None  # noqa: N815
    cor: str | None = None

    @model_serializer(mode="wrap")
    def _omite_nao_preenchidos(self, handler):
        return _sem_os_nao_preenchidos(
            self,
            handler(self),
            ("componentes", "codigoMes", "ncm", "tipoItemSped", "grupoErp", "codigoNfe", "cor"),
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
    # Liberam as travas de remoção/substituição de composição do Flow. Só com
    # dryRun=false e só pela rota manual (o diário nunca manda).
    confirmarRemocaoComposicao: bool | None = None  # noqa: N815
    confirmarSubstituicaoComposicao: bool | None = None  # noqa: N815

    @model_serializer(mode="wrap")
    def _omite_nao_preenchidos(self, handler):
        return _sem_os_nao_preenchidos(
            self, handler(self), ("confirmarRemocaoComposicao", "confirmarSubstituicaoComposicao")
        )


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
