"""Contrato `catalogo.materiais.v1` (Portal → Flow), fechado com o Flow em 16/10/2026."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from app.integrations.flowpcp.catalogo_schema import CatalogoGrupoErp, CatalogoOrigem

_OPCIONAIS = ("ncm", "tipoItemSped", "ncmUltimaEntrada", "ultimaEntradaEm")


class MaterialItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    erpProdutoId: str  # noqa: N815
    codigo: str
    nome: str = Field(min_length=1, max_length=255)
    unidadeErp: str | None = Field(default=None, max_length=16)  # noqa: N815
    ncm: str | None = Field(default=None, pattern=r"^\d{8}$")
    tipoItemSped: int | None = None  # noqa: N815
    grupoErp: CatalogoGrupoErp  # noqa: N815
    ean: str | None = Field(default=None, max_length=32)
    ativo: bool
    exigeAprovacao: bool  # noqa: N815
    ncmUltimaEntrada: str | None = Field(default=None, pattern=r"^\d{8}$")  # noqa: N815
    ultimaEntradaEm: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")  # noqa: N815

    @model_serializer(mode="wrap")
    def _omite(self, handler):
        corpo = handler(self)
        for campo in _OPCIONAIS:
            if corpo.get(campo) is None:
                corpo.pop(campo, None)
        return corpo


class MateriaisRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(default="catalogo.materiais.v1", alias="schema")
    dryRun: bool  # noqa: N815
    fullSync: bool  # noqa: N815
    itens: list[MaterialItem] = Field(min_length=1, max_length=5000)
    origem: CatalogoOrigem


class MateriaisResposta(BaseModel):
    """Relatório do Flow. `extra="allow"`: o Flow é dono do shape (resumo, amostras, capacidades)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    dry_run: bool | None = Field(default=None, alias="dryRun")
    resumo: dict = Field(default_factory=dict)
    amostras: dict = Field(default_factory=dict)
